"""
tests/test_dashboard.py
=======================
Unit tests for `crawler/dashboard.py`.
Since the UI involves endless while loops and blocking stdin, 
we mostly test that _render() generates correct output without crashing.
"""

from unittest.mock import MagicMock, patch

from crawler.coordinator import Config, Coordinator, CrawlStats, WorkItem
from crawler.dashboard import Dashboard
from crawler.index import Index
from crawler.search import ResultTriple, SearchEngine


def test_dashboard_renders_without_crashing() -> None:
    """Verifies that the _render method prints the UI successfully."""
    cfg = Config(seeds=["https://example.com"], queue_cap=100)
    idx = Index()
    idx.put(MagicMock(url="http://example.com/page", title="Test Title", depth=0, text=""))

    coord = MagicMock(spec=Coordinator)
    stats = CrawlStats()
    stats.increment_processed()
    coord.stats.return_value = stats
    
    q_mock = MagicMock()
    q_mock.qsize.return_value = 5
    coord.queue_ref.return_value = q_mock

    search_engine = MagicMock(spec=SearchEngine)
    search_engine.query.return_value = [
        ResultTriple(url="http://example.com/page", origin_url="", depth=0, score=2)
    ]

    dashboard = Dashboard(coord, idx, search_engine, cfg)

    # Patch stdout so we don't actually print to terminal during tests
    with patch("sys.stdout") as mock_stdout:
        # First render without any search
        dashboard._render()
        assert mock_stdout.write.called

        # Now simulate a search query having been executed
        dashboard._last_query = "keyword"
        dashboard._last_results = search_engine.query("keyword")
        dashboard._render()
        assert mock_stdout.write.called
