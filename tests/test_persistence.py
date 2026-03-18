"""
tests/test_persistence.py
=========================
Unit tests for crawler/persistence.py.

Coverage (per .antigravity TEST_RULES):
  ✅ append() + load_all() round-trips PageRecord correctly
  ✅ load_all() on missing file returns [] (no FileNotFoundError)
  ✅ Concurrent append() calls do not corrupt the JSONL file
  ✅ Multiple records survive a full round-trip in correct order
  ✅ Malformed lines are skipped, valid lines still returned
  ✅ Blank lines in file are silently skipped
  ✅ load_all() correctly restores datetime with timezone info
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crawler.index import PageRecord
from crawler.persistence import Persistence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_record(
    url: str = "https://example.com",
    origin_url: str = "",
    depth: int = 0,
    title: str = "Test Page",
    text: str = "Some body text",
) -> PageRecord:
    return PageRecord(
        url=url,
        origin_url=origin_url,
        depth=depth,
        title=title,
        text=text,
        indexed_at=datetime(2026, 3, 18, 10, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# load_all() on missing file
# ---------------------------------------------------------------------------

class TestLoadAllMissingFile:
    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        """load_all() must not raise if file does not exist."""
        store = Persistence(tmp_path / "nonexistent.jsonl")
        result = store.load_all()
        assert result == []

    def test_missing_file_does_not_create_file(self, tmp_path: Path) -> None:
        """load_all() must not create the file as a side effect."""
        path = tmp_path / "nonexistent.jsonl"
        Persistence(path).load_all()
        assert not path.exists()


# ---------------------------------------------------------------------------
# append() + load_all() round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_single_record_round_trips(self, tmp_path: Path) -> None:
        """One append → one load_all returns exactly that record."""
        store = Persistence(tmp_path / "index.jsonl")
        rec   = make_record()
        store.append(rec)

        loaded = store.load_all()
        assert len(loaded) == 1
        r = loaded[0]
        assert r.url        == rec.url
        assert r.origin_url == rec.origin_url
        assert r.depth      == rec.depth
        assert r.title      == rec.title
        assert r.text       == rec.text

    def test_indexed_at_datetime_restored_correctly(self, tmp_path: Path) -> None:
        """indexed_at must be deserialised back to an aware datetime."""
        store = Persistence(tmp_path / "index.jsonl")
        rec   = make_record()
        store.append(rec)

        loaded = store.load_all()
        assert len(loaded) == 1
        assert isinstance(loaded[0].indexed_at, datetime)
        # The UTC offset must be preserved (aware datetime)
        assert loaded[0].indexed_at.utcoffset() is not None
        assert loaded[0].indexed_at == rec.indexed_at

    def test_multiple_records_round_trip_in_order(self, tmp_path: Path) -> None:
        """N appends → load_all returns all N records in file order."""
        store   = Persistence(tmp_path / "index.jsonl")
        records = [
            make_record(url=f"https://example.com/{i}", depth=i, title=f"Page {i}")
            for i in range(10)
        ]
        for r in records:
            store.append(r)

        loaded = store.load_all()
        assert len(loaded) == 10
        # Order is preserved (JSONL is append-only)
        for original, restored in zip(records, loaded):
            assert restored.url   == original.url
            assert restored.depth == original.depth
            assert restored.title == original.title

    def test_append_is_cumulative_across_instances(self, tmp_path: Path) -> None:
        """A second Persistence instance pointing to the same file sees
        all records written by the first instance."""
        path = tmp_path / "index.jsonl"
        store1 = Persistence(path)
        store2 = Persistence(path)

        store1.append(make_record(url="https://a.com"))
        store2.append(make_record(url="https://b.com"))

        loaded = Persistence(path).load_all()
        urls   = {r.url for r in loaded}
        assert urls == {"https://a.com", "https://b.com"}

    def test_all_fields_are_preserved(self, tmp_path: Path) -> None:
        """Every PageRecord field survives the serialisation cycle."""
        store = Persistence(tmp_path / "index.jsonl")
        rec   = PageRecord(
            url="https://deep.example.com/path",
            origin_url="https://example.com/",
            depth=3,
            title="Deep Page",
            text="Content here",
            indexed_at=datetime(2026, 3, 18, 9, 30, 0, tzinfo=timezone.utc),
        )
        store.append(rec)
        loaded = store.load_all()[0]
        assert loaded.url        == rec.url
        assert loaded.origin_url == rec.origin_url
        assert loaded.depth      == rec.depth
        assert loaded.title      == rec.title
        assert loaded.text       == rec.text
        assert loaded.indexed_at == rec.indexed_at


# ---------------------------------------------------------------------------
# Resilience — malformed / blank lines
# ---------------------------------------------------------------------------

class TestResilienceToMalformedLines:
    def test_malformed_line_skipped_valid_lines_returned(
        self, tmp_path: Path
    ) -> None:
        """A corrupt JSON line in the middle must not prevent reading others."""
        path = tmp_path / "index.jsonl"
        store = Persistence(path)

        store.append(make_record(url="https://good-before.com"))

        # Inject a corrupt line directly
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{ this is not valid json }\n")

        store.append(make_record(url="https://good-after.com"))

        loaded = store.load_all()
        urls   = {r.url for r in loaded}
        assert "https://good-before.com" in urls
        assert "https://good-after.com"  in urls
        assert len(loaded) == 2           # corrupt line silently skipped

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        """Blank lines in the file must not cause errors."""
        path = tmp_path / "index.jsonl"
        store = Persistence(path)
        store.append(make_record())

        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")   # inject blank lines

        loaded = store.load_all()
        assert len(loaded) == 1

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        """An existing but completely empty file gives [] without error."""
        path = tmp_path / "index.jsonl"
        path.touch()                     # create empty file
        loaded = Persistence(path).load_all()
        assert loaded == []


# ---------------------------------------------------------------------------
# Concurrency — multiple threads appending simultaneously
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_appends_no_data_corruption(self, tmp_path: Path) -> None:
        """20 threads each append 25 records; all 500 must survive load_all()."""
        path       = tmp_path / "index.jsonl"
        store      = Persistence(path)
        num_threads  = 20
        records_each = 25

        def writer(thread_id: int) -> None:
            for i in range(records_each):
                store.append(
                    make_record(url=f"https://t{thread_id}.example.com/{i}")
                )

        threads = [
            threading.Thread(target=writer, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        loaded = store.load_all()
        assert len(loaded) == num_threads * records_each

    def test_concurrent_appends_produces_valid_jsonl(self, tmp_path: Path) -> None:
        """Every line written by concurrent threads must be valid JSON."""
        path  = tmp_path / "index.jsonl"
        store = Persistence(path)

        def writer(tid: int) -> None:
            for i in range(10):
                store.append(make_record(url=f"https://t{tid}.com/{i}"))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every line in the file must be parseable JSON
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    pytest.fail(
                        f"Line {line_no} is not valid JSON after concurrent writes: {exc}"
                    )
