"""
tests/test_search.py
====================
Unit tests for `crawler/search.py`.

Coverage (per .antigravity TEST_RULES):
  ✅ query() returns list of ResultTriple with correct (url, origin_url, depth)
  ✅ Title match scores higher than body-only match
  ✅ Results sorted DESC by score, ASC by depth on tie
  ✅ query() on empty index returns [] (no exception)
"""

from datetime import datetime, timezone

import pytest

from crawler.index import Index, PageRecord
from crawler.search import ResultTriple, SearchEngine


def make_record(
    url: str,
    title: str,
    text: str,
    depth: int = 0
) -> PageRecord:
    return PageRecord(
        url=url,
        origin_url="https://origin.com",
        depth=depth,
        title=title,
        text=text,
        indexed_at=datetime.now(timezone.utc)
    )


class TestSearchEngine:
    def test_query_on_empty_index_returns_empty_list(self) -> None:
        idx = Index()
        engine = SearchEngine(idx)
        results = engine.query("anything")
        assert results == []

    def test_empty_query_returns_empty_list(self) -> None:
        idx = Index()
        idx.put(make_record("url1", "title", "text"))
        engine = SearchEngine(idx)
        assert engine.query("") == []
        assert engine.query("   ") == []

    def test_query_returns_result_triple_format(self) -> None:
        idx = Index()
        idx.put(make_record("https://target.com", "The Target Page", "Body text with keyword"))
        engine = SearchEngine(idx)

        results = engine.query("keyword")
        assert len(results) == 1

        res = results[0]
        assert isinstance(res, ResultTriple)
        assert res.url == "https://target.com"
        assert res.origin_url == "https://origin.com"
        assert res.depth == 0
        assert res.score == 1  # 0 from title, 1 from body

    def test_title_match_scores_higher_than_body_match(self) -> None:
        idx = Index()

        # 1 body hit -> score 1
        idx.put(make_record("url1", "Some Page", "Here is the python keyword"))

        # 1 title hit -> score 2
        idx.put(make_record("url2", "Python Programming", "Nothing here about the language directly"))

        engine = SearchEngine(idx)
        results = engine.query("python")

        assert len(results) == 2

        # url2 should be first because its score is 2 (> 1)
        assert results[0].url == "url2"
        assert results[0].score == 2

        assert results[1].url == "url1"
        assert results[1].score == 1

    def test_sorts_descending_by_score_ascending_by_depth_on_tie(self) -> None:
        idx = Index()

        # Both have the word "apple" once in the title -> score = 2
        # url1 is depth 2, url2 is depth 1. url2 should win the tiebreak.
        idx.put(make_record("url1", "Apple Store", "Buy phones", depth=2))
        idx.put(make_record("url2", "Apple Support", "Get help", depth=1))

        # This one has "apple" in title and body -> score = 3
        # Should be absolute first despite highest depth
        idx.put(make_record("url3", "Apple Inc", "About apple", depth=5))

        engine = SearchEngine(idx)
        results = engine.query("apple")

        assert len(results) == 3

        # Highest score first
        assert results[0].url == "url3"
        assert results[0].score == 3

        # Tie on score=2, lowest depth first
        assert results[1].url == "url2"
        assert results[1].score == 2
        assert results[1].depth == 1

        assert results[2].url == "url1"
        assert results[2].score == 2
        assert results[2].depth == 2

    def test_limits_results(self) -> None:
        idx = Index()
        for i in range(10):
            idx.put(make_record(f"url{i}", f"Keyword page {i}", "text"))

        engine = SearchEngine(idx)
        results = engine.query("keyword", limit=3)
        assert len(results) == 3
