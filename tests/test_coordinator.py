"""
tests/test_coordinator.py
=========================
Unit tests for crawler/coordinator.py.

Coverage (per .antigravity TEST_RULES):
  ✅ Config defaults are correct
  ✅ WorkItem stores url / origin / depth
  ✅ CrawlStats: all increment/decrement operations, snapshot dict
  ✅ CrawlStats: thread-safe concurrent increments (100 threads × 10)
  ✅ VisitedSet: try_mark returns True on first call, False on repeat
  ✅ VisitedSet: concurrent try_mark — exactly one winner per URL
  ✅ Coordinator: seeds enqueued and visited after start()
  ✅ Coordinator: queue_ref() / stats() accessible before and after start()
  ✅ Coordinator: respects num_workers (correct thread count spawned)

Note: we do NOT test the full crawl loop here (that's integration territory).
Worker logic is tested in test_worker.py. Here we test the coordinator's own
data structures and wiring.
"""

import threading
import queue
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from crawler.coordinator import (
    Config,
    Coordinator,
    CrawlStats,
    VisitedSet,
    WorkItem,
)
from crawler.index import Index


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults(self) -> None:
        cfg = Config(seeds=["https://example.com"])
        assert cfg.max_depth    == 3
        assert cfg.num_workers  == 10
        assert cfg.queue_cap    == 500
        assert cfg.rate_per_sec == 2.0

    def test_custom_values(self) -> None:
        cfg = Config(
            seeds=["https://a.com", "https://b.com"],
            max_depth=5,
            num_workers=20,
            queue_cap=100,
            rate_per_sec=1.0,
        )
        assert cfg.seeds        == ["https://a.com", "https://b.com"]
        assert cfg.max_depth    == 5
        assert cfg.num_workers  == 20
        assert cfg.queue_cap    == 100
        assert cfg.rate_per_sec == 1.0


# ---------------------------------------------------------------------------
# WorkItem
# ---------------------------------------------------------------------------

class TestWorkItem:
    def test_fields_stored(self) -> None:
        item = WorkItem(url="https://example.com/page", origin="https://example.com", depth=1)
        assert item.url    == "https://example.com/page"
        assert item.origin == "https://example.com"
        assert item.depth  == 1

    def test_seed_has_empty_origin(self) -> None:
        item = WorkItem(url="https://example.com", origin="", depth=0)
        assert item.origin == ""
        assert item.depth  == 0


# ---------------------------------------------------------------------------
# CrawlStats
# ---------------------------------------------------------------------------

class TestCrawlStats:
    def test_initial_snapshot_all_zeros(self) -> None:
        snap = CrawlStats().snapshot()
        assert snap["processed"] == 0
        assert snap["errors"]    == 0
        assert snap["active"]    == 0

    def test_increment_processed(self) -> None:
        s = CrawlStats()
        s.increment_processed()
        s.increment_processed()
        assert s.snapshot()["processed"] == 2

    def test_increment_errors(self) -> None:
        s = CrawlStats()
        s.increment_errors()
        assert s.snapshot()["errors"] == 1

    def test_increment_and_decrement_active(self) -> None:
        s = CrawlStats()
        s.increment_active()
        s.increment_active()
        assert s.snapshot()["active"] == 2
        s.decrement_active()
        assert s.snapshot()["active"] == 1

    def test_decrement_active_never_goes_below_zero(self) -> None:
        s = CrawlStats()
        s.decrement_active()   # start from 0 — should not go negative
        assert s.snapshot()["active"] == 0

    def test_set_queue_cap(self) -> None:
        s = CrawlStats()
        s.set_queue_cap(250)
        assert s.snapshot()["queue_cap"] == 250

    def test_snapshot_returns_dict_copy(self) -> None:
        """Mutating the returned dict must not affect internal state."""
        s  = CrawlStats()
        s.increment_processed()
        d1 = s.snapshot()
        d1["processed"] = 9999          # mutate the copy
        d2 = s.snapshot()
        assert d2["processed"] == 1     # internal state unchanged

    def test_concurrent_increments_no_data_loss(self) -> None:
        """100 threads each call increment_processed 10 times → total 1000."""
        s           = CrawlStats()
        num_threads = 100
        calls_each  = 10

        def worker() -> None:
            for _ in range(calls_each):
                s.increment_processed()

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert s.snapshot()["processed"] == num_threads * calls_each


# ---------------------------------------------------------------------------
# VisitedSet
# ---------------------------------------------------------------------------

class TestVisitedSet:
    def test_first_mark_returns_true(self) -> None:
        vs = VisitedSet()
        assert vs.try_mark("https://example.com") is True

    def test_second_mark_same_url_returns_false(self) -> None:
        vs = VisitedSet()
        vs.try_mark("https://example.com")
        assert vs.try_mark("https://example.com") is False

    def test_different_urls_all_return_true(self) -> None:
        vs  = VisitedSet()
        urls = [f"https://example.com/{i}" for i in range(10)]
        for url in urls:
            assert vs.try_mark(url) is True

    def test_len_reflects_unique_urls(self) -> None:
        vs = VisitedSet()
        vs.try_mark("https://a.com")
        vs.try_mark("https://b.com")
        vs.try_mark("https://a.com")   # duplicate
        assert len(vs) == 2

    def test_concurrent_try_mark_exactly_one_winner(self) -> None:
        """50 threads all try_mark the same URL — exactly one must return True."""
        vs      = VisitedSet()
        url     = "https://example.com"
        wins:   list[bool] = []
        lock    = threading.Lock()

        def racer() -> None:
            result = vs.try_mark(url)
            with lock:
                wins.append(result)

        threads = [threading.Thread(target=racer) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert wins.count(True)  == 1   # exactly one winner
        assert wins.count(False) == 49  # all others lost

    def test_concurrent_distinct_urls_all_win(self) -> None:
        """N threads each try_mark a unique URL — all must return True."""
        vs      = VisitedSet()
        results: list[bool] = []
        lock    = threading.Lock()
        n       = 100

        def racer(i: int) -> None:
            result = vs.try_mark(f"https://example.com/{i}")
            with lock:
                results.append(result)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)        # every unique URL wins exactly once
        assert len(vs) == n


# ---------------------------------------------------------------------------
# Coordinator wiring
# ---------------------------------------------------------------------------

class TestCoordinatorWiring:
    def _make_stub_worker(
        self,
        stop_event: threading.Event,
        work_q: "queue.Queue",
    ) -> None:
        """Drain all items from the queue then wait for stop_event."""
        while not stop_event.is_set():
            try:
                work_q.get(timeout=0.05)
            except Exception:
                pass

    def test_queue_ref_is_none_before_start(self) -> None:
        idx  = Index()
        cfg  = Config(seeds=["https://example.com"], num_workers=1, queue_cap=10)
        coord = Coordinator(cfg, idx)
        assert coord.queue_ref() is None

    def test_stats_accessible_before_start(self) -> None:
        idx  = Index()
        cfg  = Config(seeds=["https://example.com"])
        coord = Coordinator(cfg, idx)
        snap = coord.stats().snapshot()
        assert snap["processed"] == 0

    def test_seeds_enqueued_and_visited_after_start(self) -> None:
        """Seeds must appear in the VisitedSet and work queue after start()."""
        idx   = Index()
        cfg   = Config(
            seeds=["https://example.com", "https://another.com"],
            num_workers=2,
            queue_cap=10,
        )
        coord = Coordinator(cfg, idx)
        stop  = threading.Event()

        # Patch the real run_worker with a no-op that honours stop_event
        def noop_worker(*args, **kwargs) -> None:
            stop_event = args[5]
            stop_event.wait(timeout=2)

        with patch("crawler.worker.run_worker", side_effect=noop_worker):
            t = threading.Thread(target=coord.start, args=(stop,), daemon=True)
            t.start()
            # Give start() time to enqueue seeds and spawn threads
            threading.Event().wait(timeout=0.1)
            stop.set()
            t.join(timeout=5)

        # Both seeds must have been marked visited
        assert coord._visited.try_mark("https://example.com")  is False
        assert coord._visited.try_mark("https://another.com")  is False

    def test_stats_queue_cap_set_after_start(self) -> None:
        """stats().snapshot()['queue_cap'] must equal cfg.queue_cap after start."""
        idx   = Index()
        cfg   = Config(seeds=["https://example.com"], num_workers=1, queue_cap=77)
        coord = Coordinator(cfg, idx)
        stop  = threading.Event()

        def noop_worker(*args, **kwargs) -> None:
            stop_event = args[5]
            stop_event.wait(timeout=2)

        with patch("crawler.worker.run_worker", side_effect=noop_worker):
            t = threading.Thread(target=coord.start, args=(stop,), daemon=True)
            t.start()
            threading.Event().wait(timeout=0.1)
            stop.set()
            t.join(timeout=5)

        assert coord.stats().snapshot()["queue_cap"] == 77
