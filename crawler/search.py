"""
crawler/search.py
=================
Search engine — provides term-frequency (TF) scoring over the indexed pages.

Role in the system
------------------
The Dashboard calls query() when the user enters a search term.
The SearchEngine reads a safe snapshot of the Index (via idx.all()) so it
never blocks the crawler threads from writing new pages. It then scores
all pages, sorts them, and returns the top N results.

Scoring Algorithm
-----------------
Tokens are extracted by repeatedly splitting on whitespace and lowercasing.
  score = (2 * title_hits) + body_hits
Results are sorted descending by score, and then ascending by depth
to prefer pages closer to the seed on a tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawler.index import Index


@dataclass
class ResultTriple:
    """A search result consisting of the URL, its referrer, its depth, and its score."""
    url: str
    origin_url: str
    depth: int
    score: int


class SearchEngine:
    """In-memory search engine providing ranked queries over the crawling Index.

    Parameters
    ----------
    idx : Index
        The shared document store. The engine will call idx.all()
        to operate on an independent memory snapshot, ensuring zero
        contention with the active crawler threads.
    """

    def __init__(self, idx: "Index") -> None:
        self._idx = idx

    def query(self, query_str: str, limit: int = 20) -> list[ResultTriple]:
        """Score all pages in the index against the given query string.

        Parameters
        ----------
        query_str : str
            Space-delimited search keywords.
        limit : int, optional
            Maximum number of results to return (default 20).

        Returns
        -------
        list[ResultTriple]
            Ranked search results.
        """
        if not query_str.strip():
            return []

        tokens = query_str.lower().split()
        if not tokens:  # pragma: no cover
            return []

        # Operating on a point-in-time snapshot so we don't lock the index
        records = self._idx.all()
        if not records:
            return []

        results: list[ResultTriple] = []

        for record in records:
            title_lower = record.title.lower()
            text_lower  = record.text.lower()

            title_hits = sum(1 for t in tokens if t in title_lower)
            body_hits  = sum(1 for t in tokens if t in text_lower)

            score = (2 * title_hits) + body_hits

            if score > 0:
                results.append(ResultTriple(
                    url=record.url,
                    origin_url=record.origin_url,
                    depth=record.depth,
                    score=score
                ))

        # Sort: Highest score first (-r.score), then shortest depth first (r.depth)
        results.sort(key=lambda r: (-r.score, r.depth))

        return results[:limit]
