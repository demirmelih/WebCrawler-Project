"""
crawler/search.py
=================
Search engine — provides frequency-based scoring over the indexed pages.

Scoring Algorithm
-----------------
For each word in the query, we look up matching entries from data/storage/p.data.
Each entry contains (word, url, origin, depth, frequency).

   score = (frequency × 10) + 1000 (exact match bonus) - (depth × 5)

Results are sorted descending by score. If multiple entries exist for the same
URL (from different query words), we take the highest individual score for that URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawler.index import Index
    from crawler.persistence import PDataWriter


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
        The shared document store.
    pdata : PDataWriter | None
        Optional PDataWriter for frequency-based scoring from p.data.
    """

    def __init__(self, idx: "Index", pdata: "PDataWriter | None" = None) -> None:
        self._idx = idx
        self._pdata = pdata

    def query(self, query_str: str, limit: int = 20, sort_by: str = "relevance") -> list[ResultTriple]:
        """Score pages against the query using the frequency-based formula.

        Formula: score = (frequency × 10) + 1000 - (depth × 5)

        Parameters
        ----------
        query_str : str
            Space-delimited search keywords.
        limit : int, optional
            Maximum number of results to return (default 20).
        sort_by : str, optional
            Sorting strategy. Currently only 'relevance' (default).

        Returns
        -------
        list[ResultTriple]
            Ranked search results.
        """
        if not query_str.strip():
            return []

        tokens = query_str.lower().split()
        if not tokens:
            return []

        # Strategy: use p.data file if available for frequency-based scoring
        if self._pdata:
            return self._query_pdata(tokens, limit)

        # Fallback: in-memory scan (legacy mode)
        return self._query_inmemory(tokens, limit)

    def _query_pdata(self, tokens: list[str], limit: int) -> list[ResultTriple]:
        """Score using the p.data word-frequency file.

        Formula: score = (frequency × 10) + 1000 - (depth × 5)
        """
        # For each URL, track the best score and metadata
        best: dict[str, ResultTriple] = {}

        for token in tokens:
            entries = self._pdata.load_word_entries(token)
            for entry in entries:
                score = (entry["frequency"] * 10) + 1000 - (entry["depth"] * 5)

                url = entry["url"]
                if url not in best or score > best[url].score:
                    best[url] = ResultTriple(
                        url=url,
                        origin_url=entry["origin"],
                        depth=entry["depth"],
                        score=score,
                    )

        results = list(best.values())
        results.sort(key=lambda r: (-r.score, r.depth))
        return results[:limit]

    def _query_inmemory(self, tokens: list[str], limit: int) -> list[ResultTriple]:
        """Fallback: scan the in-memory index when p.data is unavailable."""
        records = self._idx.all()
        if not records:
            return []

        results: list[ResultTriple] = []

        for record in records:
            title_lower = record.title.lower()
            text_lower = record.text.lower()

            title_hits = sum(1 for t in tokens if t in title_lower)
            body_hits = sum(1 for t in tokens if t in text_lower)
            score = (2 * title_hits) + body_hits

            if score > 0:
                results.append(ResultTriple(
                    url=record.url,
                    origin_url=record.origin_url,
                    depth=record.depth,
                    score=score,
                ))

        results.sort(key=lambda r: (-r.score, r.depth))
        return results[:limit]
