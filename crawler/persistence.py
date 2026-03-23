"""
crawler/persistence.py
======================
Persistence layer — writes crawled page data to disk in two formats:

1. `data/storage/p.data`  — Word-frequency index (one line per word per page).
   Format: word url origin depth frequency

2. `index.jsonl`          — Full PageRecord archive (optional, CLI-only).

Thread Safety
-------------
All file writes are protected by threading.Lock to prevent interleaved lines
from concurrent worker threads.
"""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawler.index import PageRecord

# ---------------------------------------------------------------------------
# Word tokenizer — stdlib only, no NLTK / spaCy
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Extract lowercase alphanumeric tokens from text."""
    return _WORD_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# PDataWriter — writes word-frequency tuples to data/storage/p.data
# ---------------------------------------------------------------------------

class PDataWriter:
    """Append-only writer for the word-frequency index file.

    Each call to write() tokenizes the page's title + body text, counts
    word frequencies, and appends one line per unique word:

        word url origin depth frequency

    The file is created (along with parent directories) on first write.
    All operations are guarded by a threading.Lock.
    """

    def __init__(self, filepath: str = "data/storage/p.data") -> None:
        self._path = Path(filepath)
        self._lock = threading.Lock()
        # Ensure parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: "PageRecord") -> None:
        """Tokenize a PageRecord and append word-frequency lines to p.data."""
        # Combine title and body for frequency counting
        combined_text = f"{record.title} {record.text}"
        tokens = tokenize(combined_text)

        if not tokens:
            return

        freq_map: Counter = Counter(tokens)
        origin = record.origin_url if record.origin_url else ""

        lines: list[str] = []
        for word, count in freq_map.items():
            # Format: word url origin depth frequency
            lines.append(f"{word} {record.url} {origin} {record.depth} {count}\n")

        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.writelines(lines)

    def load_word_entries(self, query_word: str) -> list[dict]:
        """Read p.data and return all entries matching the given word.

        Returns a list of dicts: {word, url, origin, depth, frequency}.
        """
        query_word = query_word.lower().strip()
        if not query_word or not self._path.exists():
            return []

        results: list[dict] = []
        with self._lock:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split(" ", 4)
                    if len(parts) < 5:
                        continue
                    word, url, origin, depth_str, freq_str = parts
                    if word == query_word:
                        try:
                            results.append({
                                "word": word,
                                "url": url,
                                "origin": origin,
                                "depth": int(depth_str),
                                "frequency": int(freq_str),
                            })
                        except ValueError:
                            continue
        return results

    def clear(self) -> None:
        """Remove the p.data file (useful for fresh crawls)."""
        with self._lock:
            if self._path.exists():
                self._path.unlink()
