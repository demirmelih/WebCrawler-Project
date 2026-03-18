"""
crawler/coordinator.py
======================
Orchestration layer — owns the work queue, worker pool, VisitedSet, and
CrawlStats. Every other module plugs into what's defined here.

Role in the system
------------------

  main.py  ──► Coordinator(cfg, idx)
                    │
                    ├── Creates queue.Queue(maxsize=cfg.queue_cap)
                    ├── Creates VisitedSet
                    ├── Creates CrawlStats
                    ├── Enqueues seed WorkItems at depth=0
                    └── Spawns N daemon Threads running worker.run_worker()

  Workers consume WorkItems from the queue, write to the shared Index,
  then enqueue child WorkItems (blocking when queue is full — back-pressure).

  Dashboard calls coordinator.stats().snapshot() every second.

Concurrency model
-----------------
  queue.Queue(maxsize=M)
    ├── put(block=True) in workers → blocks when full (natural back-pressure)
    └── get(timeout=1)  in workers → raises queue.Empty if idle

  VisitedSet: threading.Lock + set[str]
    └── try_mark() is atomic: check-and-insert under one lock acquisition

  CrawlStats: threading.Lock + plain int counters (Python has no atomic int)
    └── snapshot() returns a plain dict — safe to read from Dashboard thread

Constraints (from .antigravity PROMPT_COORDINATOR)
---------------------------------------------------
  * ONLY stdlib: threading, queue, dataclasses.
  * Imports: crawler.index only (worker is imported lazily inside start()
    to avoid circular imports at module load time).
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for type hints — no runtime circular dependency
    from crawler.index import Index

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """All user-supplied settings for one crawl run.

    Attributes
    ----------
    seeds        : One or more seed URLs to start crawling from.
    max_depth    : Maximum BFS depth from seed. Seed itself is depth 0.
    num_workers  : Number of concurrent worker threads.
    queue_cap    : Maximum capacity of the bounded work queue (back-pressure).
    rate_per_sec : Maximum HTTP requests per second per domain.
                   Workers sleep to enforce this limit.
    """

    seeds:        list[str]
    max_depth:    int   = 3
    num_workers:  int   = 10
    queue_cap:    int   = 500
    rate_per_sec: float = 2.0


# ---------------------------------------------------------------------------
# Work Item
# ---------------------------------------------------------------------------

@dataclass
class WorkItem:
    """A single unit of crawl work: one URL to fetch.

    Attributes
    ----------
    url    : Absolute URL to fetch.
    origin : URL of the page that linked to this one. Empty string for seeds.
    depth  : Crawl depth from the seed (seed = 0).
    """

    url:    str
    origin: str
    depth:  int


# ---------------------------------------------------------------------------
# CrawlStats — thread-safe counters
# ---------------------------------------------------------------------------

class CrawlStats:
    """Thread-safe snapshot counters for dashboard and monitoring.

    All mutation methods acquire _lock before touching integer fields.
    snapshot() returns a plain dict — safe to consume outside the lock.

    Note: Python has no atomic integers (unlike Go's sync/atomic).
    We use a single threading.Lock for all counters — low-contention
    because increments are O(1) and the lock is held only for that.
    """

    def __init__(self) -> None:
        self._lock          = threading.Lock()
        self._processed:    int  = 0
        self._errors:       int  = 0
        self._active:       int  = 0   # workers currently fetching a page
        self._queue_cap:    int  = 0   # set by Coordinator after queue creation

    # ------------------------------------------------------------------
    # Mutators (called from worker threads)
    # ------------------------------------------------------------------

    def increment_processed(self) -> None:
        with self._lock:
            self._processed += 1

    def increment_errors(self) -> None:
        with self._lock:
            self._errors += 1

    def increment_active(self) -> None:
        with self._lock:
            self._active += 1

    def decrement_active(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def set_queue_cap(self, cap: int) -> None:
        with self._lock:
            self._queue_cap = cap

    # ------------------------------------------------------------------
    # Accessor (called from Dashboard thread)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a point-in-time copy of all counters as a plain dict.

        Safe to read from any thread — no lock is held after return.
        """
        with self._lock:
            return {
                "processed":   self._processed,
                "errors":      self._errors,
                "active":      self._active,
                "queue_cap":   self._queue_cap,
            }

    def __repr__(self) -> str:  # pragma: no cover
        snap = self.snapshot()
        return (
            f"CrawlStats(processed={snap['processed']}, "
            f"errors={snap['errors']}, active={snap['active']})"
        )


# ---------------------------------------------------------------------------
# VisitedSet — thread-safe deduplication gate
# ---------------------------------------------------------------------------

class VisitedSet:
    """Thread-safe set of crawled URLs.

    The key correctness property: try_mark() is atomic.
    A URL is checked AND inserted in a single lock hold, so two workers
    racing on the same URL cannot both get True.

    Usage
    -----
    if visited.try_mark(url):
        # we are the first to see this URL — safe to enqueue
        work_q.put(WorkItem(url, origin, depth))
    # else: already seen, skip silently
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._seen: set[str]       = set()

    def try_mark(self, url: str) -> bool:
        """Mark *url* as visited.

        Returns
        -------
        True  — if url was NOT previously seen (first visit; caller should enqueue).
        False — if url was already in the set (duplicate; caller should skip).
        """
        with self._lock:
            if url in self._seen:
                return False
            self._seen.add(url)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def __repr__(self) -> str:  # pragma: no cover
        return f"VisitedSet(seen={len(self)})"


# ---------------------------------------------------------------------------
# Coordinator — orchestrates the entire crawl lifecycle
# ---------------------------------------------------------------------------

class Coordinator:
    """Wires the work queue, worker pool, and shared state together.

    Lifecycle
    ---------
    1.  coord = Coordinator(cfg, idx)
    2.  coord.start(stop_event)          # blocks until stop_event is set
    3.  stop_event.set()                 # triggered by SIGINT in main.py

    After start() returns all worker threads have joined cleanly.

    Parameters
    ----------
    cfg : Config   — crawler settings.
    idx : Index    — shared page store (written by workers, read by searcher).
    """

    def __init__(self, cfg: Config, idx: "Index") -> None:
        self._cfg      = cfg
        self._idx      = idx
        self._stats    = CrawlStats()
        self._visited  = VisitedSet()
        # Queue created lazily in start() so queue_cap is set exactly once.
        self._queue:   queue.Queue[WorkItem] | None = None
        self._threads: list[threading.Thread]       = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, stop_event: threading.Event) -> None:
        """Initialise queue, enqueue seeds, spawn workers, block until done.

        This method is intended to be called in its own daemon Thread from
        main.py. It blocks until stop_event is set AND all workers have
        exited cleanly.

        Workers are daemon threads, so they never prevent process exit,
        but we join() them explicitly for a clean drain.
        """
        # Lazy import to avoid circular dependency at module load time.
        from crawler.worker import run_worker  # noqa: PLC0415

        # Create the bounded work queue.
        work_q: queue.Queue[WorkItem] = queue.Queue(maxsize=self._cfg.queue_cap)
        self._queue = work_q
        self._stats.set_queue_cap(self._cfg.queue_cap)

        # Enqueue seed URLs at depth 0.
        for seed_url in self._cfg.seeds:
            if self._visited.try_mark(seed_url):
                try:
                    work_q.put(
                        WorkItem(url=seed_url, origin="", depth=0),
                        block=True,
                        timeout=5,
                    )
                    logger.info("Enqueued seed: %s", seed_url)
                except queue.Full:
                    logger.warning("Queue full when enqueuing seed %s", seed_url)

        # Spawn N daemon worker threads.
        self._threads = [
            threading.Thread(
                target=run_worker,
                args=(work_q, self._visited, self._idx,
                      self._cfg, self._stats, stop_event),
                name=f"worker-{i}",
                daemon=True,
            )
            for i in range(self._cfg.num_workers)
        ]
        for t in self._threads:
            t.start()
        logger.info(
            "Coordinator started %d workers (queue_cap=%d, max_depth=%d).",
            self._cfg.num_workers, self._cfg.queue_cap, self._cfg.max_depth,
        )

        # Block until stop_event fires, then join all workers.
        stop_event.wait()
        logger.info("Stop event received — waiting for workers to drain.")
        for t in self._threads:
            t.join(timeout=5)
        logger.info("All workers joined. Crawl finished.")

    def stats(self) -> CrawlStats:
        """Return the live CrawlStats object.

        The Dashboard calls stats().snapshot() to get a safe dict copy.
        """
        return self._stats

    def queue_ref(self) -> "queue.Queue[WorkItem] | None":
        """Return the work queue (None before start() is called).

        Dashboard uses queue_ref().qsize() for the 'Queued' counter.
        """
        return self._queue
