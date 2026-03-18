"""
crawler/worker.py
=================
Crawler worker — fetches one URL, parses HTML, writes to Index, enqueues children.

NOTE: This is a STUB created so coordinator.py can import run_worker.
      The full implementation is Phase 4 Step 4.
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawler.coordinator import Config, CrawlStats, VisitedSet
    from crawler.index import Index


def run_worker(
    work_q:     "queue.Queue",
    visited:    "VisitedSet",
    idx:        "Index",
    cfg:        "Config",
    stats:      "CrawlStats",
    stop_event: threading.Event,
) -> None:  # pragma: no cover
    """Worker stub — will be fully implemented in Step 4."""
    while not stop_event.is_set():
        try:
            work_q.get(timeout=0.5)
        except queue.Empty:
            continue
