"""
crawler/persistence.py
======================
Optional JSONL-based save/resume layer for the Web Crawler.

Role in the system
------------------
Persistence gives the crawler a "memory" between runs. When --persist is
passed on the CLI, every PageRecord written to the Index is also appended
to `index.jsonl` on disk. On the next startup, load_all() reads that file,
re-populates the Index and the VisitedSet, and the crawl resumes from where
it left off — skipping all pages already indexed.

  Worker thread ──► idx.put(record)
                         └──► persistence.append(record) ──► index.jsonl

  Next startup:
  persistence.load_all() ──► list[PageRecord] ──► re-fill idx + visited set

File format: Newline-Delimited JSON (JSONL / JSON Lines)
--------------------------------------------------------
Each line in index.jsonl is one complete JSON object:

  {"url": "https://example.com", "origin_url": "", "depth": 0,
   "title": "Example", "text": "Example Domain",
   "indexed_at": "2026-03-18T10:00:00+00:00"}

  * Append-only — safe across crashes (partial lines at EOF are skipped).
  * Human-readable — easy to inspect or grep.
  * Stream-parseable — load_all() reads line-by-line, never loading the
    whole file into memory at once.

Thread safety
-------------
A single threading.Lock (_lock) guards both append() and load_all().
This lock is completely separate from the Index lock, so a worker calling
append() never contends with another worker calling idx.put().

Constraints (from .antigravity PROMPT_PERSISTENCE)
--------------------------------------------------
  * ONLY stdlib: json, threading, pathlib, dataclasses, datetime.
  * Use dataclasses.asdict() for serialisation.
  * deserialise indexed_at as ISO string → datetime.fromisoformat().
  * load_all() on missing file → return [] (not an error).
  * Malformed lines → log warning and skip (don't crash).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from crawler.index import PageRecord

logger = logging.getLogger(__name__)

# Default file written next to wherever the process runs.
DEFAULT_PATH = "index.jsonl"


class Persistence:
    """Append-only JSONL persistence layer for PageRecord objects.

    Parameters
    ----------
    filepath : str | Path
        Path to the JSONL file. Defaults to "index.jsonl" in the CWD.
        The file (and any parent directories) will be created on first
        append if they don't exist.

    Usage
    -----
    # Setup (once, in main.py):
    store = Persistence("index.jsonl")

    # During crawl — called inside run_worker() after each successful page:
    store.append(record)

    # On startup with --persist flag — called before coordinator.start():
    for record in store.load_all():
        idx.put(record)
        visited.try_mark(record.url)
    """

    def __init__(self, filepath: str | Path = DEFAULT_PATH) -> None:
        self._path = Path(filepath)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, record: PageRecord) -> None:
        """Serialise *record* as one JSON line and append to the file.

        Opens the file in append mode, writes exactly one line, then closes
        immediately. This means:
          * Each successful call is crash-safe — a crash between two calls
            leaves a complete line on disk.
          * Concurrent threads each append their own line atomically inside
            the lock; no two threads interleave their writes.

        Parameters
        ----------
        record : PageRecord
            The page to persist.

        Raises
        ------
        OSError
            If the file cannot be opened or written (e.g. permission error).
            Let this propagate — the caller (worker) should log and continue.
        """
        # Ensure parent directories exist (e.g. if filepath = "data/index.jsonl")
        self._path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                # asdict() converts the dataclass to a plain dict.
                # default=str handles datetime → ISO-8601 string automatically.
                fh.write(json.dumps(asdict(record), default=str) + "\n")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_all(self) -> list[PageRecord]:
        """Read and deserialise every PageRecord from the JSONL file.

        Returns
        -------
        list[PageRecord]
            All records found in the file, in file order.
            Returns an empty list (not an error) if the file does not exist.
            Malformed or unreadable lines are skipped with a warning log.

        Notes
        -----
        Reads the file line by line — never loads the entire content into
        memory. Safe for arbitrarily large index files.
        """
        if not self._path.exists():
            logger.debug("Persistence file %s not found — starting fresh.", self._path)
            return []

        records: list[PageRecord] = []

        with self._lock:
            with self._path.open("r", encoding="utf-8") as fh:
                for line_no, raw in enumerate(fh, start=1):
                    raw = raw.strip()
                    if not raw:         # skip blank lines
                        continue
                    try:
                        data = json.loads(raw)
                        # Restore datetime from ISO-8601 string produced by
                        # json.dumps(..., default=str).
                        data["indexed_at"] = datetime.fromisoformat(
                            data["indexed_at"]
                        )
                        records.append(PageRecord(**data))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        logger.warning(
                            "Skipping malformed line %d in %s: %s",
                            line_no,
                            self._path,
                            exc,
                        )

        logger.info("Loaded %d records from %s.", len(records), self._path)
        return records

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The resolved path of the JSONL file (read-only)."""
        return self._path

    def __repr__(self) -> str:  # pragma: no cover
        return f"Persistence(path={self._path!r})"
