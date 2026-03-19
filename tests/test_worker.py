"""
tests/test_worker.py
====================
Unit tests for `crawler/worker.py`. Test that the worker correctly extracts
content from HTML, respects rate limiting and max depth, properly interacts
with the index/coordinator/visited structures, and handles errors gracefully.

Coverage (per .antigravity TEST_RULES):
  ✅ HTTP 200 → PageRecord written to index, child URLs enqueued
  ✅ HTTP 404 → error counted, nothing written to index
  ✅ Link at depth=k → NOT enqueued
  ✅ Relative URLs resolved to absolute before enqueue
"""

import queue
import threading
from unittest.mock import MagicMock, patch

import pytest

from crawler.coordinator import Config, CrawlStats, VisitedSet, WorkItem
from crawler.index import Index
from crawler.worker import PageParser, run_worker


# ---------------------------------------------------------------------------
# Helpers for mocking urllib.request.urlopen
# ---------------------------------------------------------------------------

class MockResponse:
    """A minimal mock for the context manager returned by urlopen()."""
    def __init__(self, html_bytes: bytes, status: int = 200, content_type: str = "text/html") -> None:
        self.html_bytes = html_bytes
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, limit: int = -1) -> bytes:
        return self.html_bytes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_deps():
    """Returns a tuple of initialized objects needed for the worker."""
    idx = Index()
    visited = VisitedSet()
    stats = CrawlStats()
    work_q = queue.Queue(maxsize=10)
    stop_event = threading.Event()
    cfg = Config(["https://example.com"], max_depth=3, rate_per_sec=0) # 0 rate for tests
    return idx, visited, stats, work_q, stop_event, cfg


# ---------------------------------------------------------------------------
# Component: PageParser
# ---------------------------------------------------------------------------

class TestPageParser:
    def test_parses_title_and_body_ignores_scripts(self) -> None:
        html = """
        <html>
            <head><title> The Title </title></head>
            <body>
                <h1>Hello</h1>
                <script> let x = 1; </script>
                <p>World</p>
                <style> body { color: red } </style>
            </body>
        </html>
        """
        parser = PageParser()
        parser.feed(html)
        assert " ".join(parser.title_parts).strip() == "The Title"
        assert " ".join(parser.text_parts).strip() == "Hello World"

    def test_extracts_links(self) -> None:
        html = """
        <a href="https://example.com/page1">1</a>
        <a href="/relative">2</a>
        <a name="anchor">No Href</a>
        """
        parser = PageParser()
        parser.feed(html)
        assert parser.links == ["https://example.com/page1", "/relative"]


# ---------------------------------------------------------------------------
# Worker Logic
# ---------------------------------------------------------------------------

class TestWorker:
    @patch("crawler.worker.urllib.request.urlopen")
    def test_http_200_writes_record_and_enqueues_children(
        self, mock_urlopen: MagicMock, mock_deps
    ) -> None:
        idx, visited, stats, work_q, stop_event, cfg = mock_deps
        mock_urlopen.return_value = MockResponse(
            b'<html><title>Test</title><a href="/child">link</a></html>'
        )

        item = WorkItem("https://example.com/", origin="", depth=0)
        work_q.put(item)
        visited.try_mark(item.url)

        # Hook idx.put so the worker stops immediately AFTER this iteration finishes
        original_put = idx.put
        def hook_put(record):
            original_put(record)
            stop_event.set()
        
        with patch.object(idx, "put", side_effect=hook_put):
            worker_thread = threading.Thread(
                target=run_worker,
                args=(work_q, visited, idx, cfg, stats, stop_event)
            )
            worker_thread.start()
            worker_thread.join(timeout=2)

        # 1. PageRecord written to index
        assert idx.size() == 1
        record = idx.get("https://example.com/")
        assert record is not None
        assert record.title == "Test"

        # 2. Child URLs enqueued (and VisitedSet updated)
        # The parent is drained, but the child is now in the queue
        assert work_q.qsize() == 1
        child_item = work_q.get()
        assert child_item.url == "https://example.com/child"
        assert child_item.origin == "https://example.com/"
        assert child_item.depth == 1

        snap = stats.snapshot()
        assert snap["processed"] == 1
        assert snap["errors"] == 0

    @patch("crawler.worker.urllib.request.urlopen")
    def test_http_404_error_counted_nothing_indexed(
        self, mock_urlopen: MagicMock, mock_deps
    ) -> None:
        idx, visited, stats, work_q, stop_event, cfg = mock_deps

        import urllib.error
        # Make the mock raise an HTTPError
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com/", code=404, msg="Not Found", hdrs={}, fp=None
        )

        work_q.put(WorkItem("https://example.com/", origin="", depth=0))

        worker_thread = threading.Thread(
            target=run_worker,
            args=(work_q, visited, idx, cfg, stats, stop_event)
        )
        worker_thread.start()

        work_q.join()
        stop_event.set()
        worker_thread.join(timeout=1)

        assert idx.size() == 0
        assert work_q.qsize() == 0

        snap = stats.snapshot()
        assert snap["processed"] == 0
        assert snap["errors"] == 1

    @patch("crawler.worker.urllib.request.urlopen")
    def test_links_at_max_depth_not_enqueued(
        self, mock_urlopen: MagicMock, mock_deps
    ) -> None:
        idx, visited, stats, work_q, stop_event, cfg = mock_deps
        cfg.max_depth = 2

        mock_urlopen.return_value = MockResponse(
            b'<html><a href="/new_link">link</a></html>'
        )

        # Put an item ALREADY at max_depth
        work_q.put(WorkItem("https://example.com/", origin="", depth=2))

        worker_thread = threading.Thread(
            target=run_worker, args=(work_q, visited, idx, cfg, stats, stop_event)
        )
        worker_thread.start()

        work_q.join()
        stop_event.set()
        worker_thread.join()

        # Page is indexed, but no children are added since we hit max depth
        assert idx.size() == 1
        assert work_q.qsize() == 0

    @patch("crawler.worker.urllib.request.urlopen")
    def test_skips_non_html_content(
        self, mock_urlopen: MagicMock, mock_deps
    ) -> None:
        idx, visited, stats, work_q, stop_event, cfg = mock_deps
        
        # Return a PDF content type mock
        mock_urlopen.return_value = MockResponse(b"PDFBYTES", content_type="application/pdf")

        work_q.put(WorkItem("https://example.com/file.pdf", origin="", depth=0))

        worker_thread = threading.Thread(
            target=run_worker, args=(work_q, visited, idx, cfg, stats, stop_event)
        )
        worker_thread.start()

        work_q.join()
        stop_event.set()
        worker_thread.join()

        # Nothing indexed, no errors (gracefully skipped)
        assert idx.size() == 0
        snap = stats.snapshot()
        assert snap["processed"] == 0
        assert snap["errors"] == 0
