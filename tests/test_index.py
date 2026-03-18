"""
tests/test_index.py
===================
Unit tests for crawler/index.py.

Coverage (per .antigravity TEST_RULES):
  ✅ put() then get() returns correct PageRecord
  ✅ size() reflects actual count after multiple puts
  ✅ all() returns a COPY (mutations don't affect internal state)
  ✅ Concurrent put() from 20 threads produces no data loss
  ✅ put() with same URL overwrites (idempotent)
  ✅ get() on missing URL returns None
  ✅ __post_init__ collapses whitespace in title and text
"""

import threading
from datetime import datetime, timezone

import pytest

from crawler.index import Index, PageRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_record(
    url: str = "https://example.com",
    origin_url: str = "",
    depth: int = 0,
    title: str = "Example Title",
    text: str = "Some body text here",
) -> PageRecord:
    """Factory for test PageRecord objects."""
    return PageRecord(
        url=url,
        origin_url=origin_url,
        depth=depth,
        title=title,
        text=text,
        indexed_at=datetime(2026, 3, 16, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Basic correctness
# ---------------------------------------------------------------------------

class TestPutAndGet:
    def test_put_then_get_returns_record(self) -> None:
        """put() stores a record; get() retrieves it by URL."""
        idx = Index()
        rec = make_record()
        idx.put(rec)
        result = idx.get(rec.url)
        assert result is not None
        assert result.url        == rec.url
        assert result.origin_url == rec.origin_url
        assert result.depth      == rec.depth
        assert result.title      == rec.title
        assert result.text       == rec.text

    def test_get_missing_url_returns_none(self) -> None:
        """get() on a URL that was never put returns None."""
        idx = Index()
        assert idx.get("https://not-here.com") is None

    def test_put_overwrites_existing_url(self) -> None:
        """Second put() for the same URL replaces the first."""
        idx = Index()
        first  = make_record(title="First")
        second = make_record(title="Second")   # same URL
        idx.put(first)
        idx.put(second)
        result = idx.get(first.url)
        assert result is not None
        assert result.title == "Second"

    def test_multiple_distinct_urls_stored_independently(self) -> None:
        idx = Index()
        r1 = make_record(url="https://a.com", title="A")
        r2 = make_record(url="https://b.com", title="B")
        idx.put(r1)
        idx.put(r2)
        assert idx.get("https://a.com").title == "A"  # type: ignore[union-attr]
        assert idx.get("https://b.com").title == "B"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# size()
# ---------------------------------------------------------------------------

class TestSize:
    def test_empty_index_has_size_zero(self) -> None:
        assert Index().size() == 0

    def test_size_increments_with_each_new_url(self) -> None:
        idx = Index()
        for i in range(5):
            idx.put(make_record(url=f"https://example.com/{i}"))
        assert idx.size() == 5

    def test_repeated_put_same_url_does_not_grow_size(self) -> None:
        idx = Index()
        rec = make_record()
        idx.put(rec)
        idx.put(rec)
        assert idx.size() == 1


# ---------------------------------------------------------------------------
# all()
# ---------------------------------------------------------------------------

class TestAll:
    def test_all_returns_all_records(self) -> None:
        idx = Index()
        records = [make_record(url=f"https://example.com/{i}") for i in range(3)]
        for r in records:
            idx.put(r)
        results = idx.all()
        assert len(results) == 3
        result_urls = {r.url for r in results}
        assert result_urls == {f"https://example.com/{i}" for i in range(3)}

    def test_all_on_empty_index_returns_empty_list(self) -> None:
        assert Index().all() == []

    def test_all_returns_copy_not_reference(self) -> None:
        """Mutating the returned list must not change the internal store."""
        idx = Index()
        idx.put(make_record(url="https://example.com"))
        snapshot = idx.all()
        snapshot.clear()                 # mutate the returned list
        assert idx.size() == 1           # internal store is unchanged
        assert idx.get("https://example.com") is not None


# ---------------------------------------------------------------------------
# PageRecord.__post_init__ whitespace normalisation
# ---------------------------------------------------------------------------

class TestPageRecordNormalisation:
    def test_title_whitespace_collapsed(self) -> None:
        rec = make_record(title="  Hello   World  ")
        assert rec.title == "Hello World"

    def test_text_whitespace_collapsed(self) -> None:
        rec = make_record(text="\n  Line one\n  Line two\n")
        assert rec.text == "Line one Line two"

    def test_empty_title_stays_empty(self) -> None:
        rec = make_record(title="")
        assert rec.title == ""


# ---------------------------------------------------------------------------
# Concurrency — 20 threads write simultaneously, no data loss
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_puts_no_data_loss(self) -> None:
        """20 threads each put 50 unique records: final size must be 1000."""
        idx        = Index()
        num_threads  = 20
        records_each = 50

        def writer(thread_id: int) -> None:
            for i in range(records_each):
                url = f"https://example.com/thread{thread_id}/page{i}"
                idx.put(make_record(url=url, title=f"T{thread_id}-P{i}"))

        threads = [
            threading.Thread(target=writer, args=(t,))
            for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert idx.size() == num_threads * records_each

    def test_concurrent_put_and_all_no_exception(self) -> None:
        """Writers and readers running simultaneously must not raise."""
        idx         = Index()
        stop_event  = threading.Event()
        errors: list[Exception] = []

        def writer() -> None:
            i = 0
            while not stop_event.is_set():
                idx.put(make_record(url=f"https://example.com/{i}"))
                i += 1

        def reader() -> None:
            while not stop_event.is_set():
                try:
                    _ = idx.all()
                except Exception as exc:
                    errors.append(exc)

        threads = (
            [threading.Thread(target=writer) for _ in range(5)]
            + [threading.Thread(target=reader) for _ in range(5)]
        )
        for t in threads:
            t.start()

        # Let them run for 0.5 seconds
        stop_event.wait(timeout=0.5)
        stop_event.set()

        for t in threads:
            t.join(timeout=2)

        assert errors == [], f"Exceptions during concurrent access: {errors}"
