"""
crawler/index.py
================
The Shared Index — thread-safe, in-memory store of every crawled PageRecord.

Role in the system
------------------
This is the single source of truth for all crawled data. Every other module
interacts with it:

  * Worker threads call put() to store a freshly crawled page.
  * SearchEngine calls all() to get a snapshot for ranking.
  * Dashboard calls size() to display how many pages have been indexed.

Concurrency model
-----------------
A single threading.RLock guards all four public methods:

  put()  — acquires the lock exclusively for the brief dict insertion, then
            immediately releases it. Workers are never blocked long.

  get()  — acquires the lock to fetch one record, releases immediately.

  all()  — acquires the lock, copies all dict values into a NEW list, then
            releases the lock. The caller gets a snapshot it can iterate
            freely without any lock held. This is the key design choice:
            scoring (in SearchEngine) happens outside the lock, so workers
            and the searcher do not block each other during scoring.

  size() — acquires the lock, reads len(), releases.

Python's threading.RLock is re-entrant (same thread can re-acquire it),
which prevents deadlocks if any method ever calls another method internally.

Constraints (from .antigravity)
--------------------------------
  * ONLY stdlib imports: dataclasses, datetime, threading, typing.
  * NO external packages.
  * All() must return a COPY — mutations by the caller must not affect the
    internal dict.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class PageRecord:
    """Immutable snapshot of one successfully crawled web page.

    Fields
    ------
    url        : Absolute, normalised URL of this page.
    origin_url : The page that contained the link to this URL.
                 Empty string "" for seed URLs (they have no parent).
    depth      : Crawl depth from the seed. Seed itself is depth 0.
    title      : Text content of the <title> tag (stripped). May be empty.
    text       : Visible body text (whitespace-collapsed). Used for scoring.
    indexed_at : UTC timestamp of when this record was written into the index.
    """

    url:        str
    origin_url: str
    depth:      int
    title:      str
    text:       str
    indexed_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def __post_init__(self) -> None:
        """Normalise whitespace in title and text at construction time."""
        self.title = " ".join(self.title.split())
        self.text  = " ".join(self.text.split())


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

class Index:
    """Thread-safe, in-memory storage for PageRecord objects.

    Keyed by URL (str). The last write for a given URL wins (idempotent put).

    Typical usage
    -------------
    idx = Index()

    # Writer (worker thread):
    idx.put(PageRecord(url="https://example.com", origin_url="", depth=0,
                       title="Example", text="Example Domain"))

    # Reader (search engine — gets a snapshot, no lock held during iteration):
    for record in idx.all():
        ...

    # Dashboard:
    print(f"Indexed: {idx.size()} pages")
    """

    def __init__(self) -> None:
        self._store: dict[str, PageRecord] = {}
        self._lock  = threading.RLock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(self, record: PageRecord) -> None:
        """Insert or overwrite the record for record.url.

        Acquires the lock exclusively for the duration of the dict write,
        then releases it immediately. Contention is minimal.
        """
        with self._lock:
            self._store[record.url] = record

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, url: str) -> Optional[PageRecord]:
        """Return the PageRecord for *url*, or None if not found."""
        with self._lock:
            return self._store.get(url)

    def all(self) -> list[PageRecord]:
        """Return a *copy* of all stored PageRecords as a list.

        The lock is held only during the copy. The returned list is
        independent of the internal dict — callers may iterate, sort,
        or filter it without holding any lock and without affecting the
        index state.
        """
        with self._lock:
            return list(self._store.values())

    def size(self) -> int:
        """Return the current number of indexed pages."""
        with self._lock:
            return len(self._store)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"Index(size={self.size()})"
