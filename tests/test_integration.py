"""
tests/test_integration.py
=========================
Integration tests for the Web Crawler.

Spins up a local Python http.server in a background thread with some interconnected
pages, runs the crawler via the Coordinator API (simulating main.py logic), and
verifies the final Index and CrawlStats end-to-end.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import pytest

from crawler.coordinator import Config, Coordinator
from crawler.index import Index
from crawler.worker import run_worker

# Simple HTML pages interconnected like a small site
PAGES = {
    "/": b"<html><title>Home</title>Welcome. <a href='/about'>About</a> <a href='/contact'>Contact</a></html>",
    "/about": b"<html><title>About Us</title>We are a test site. <a href='/'>Home</a></html>",
    "/contact": b"<html><title>Contact</title>Email us. <a href='/hidden'>Hidden</a></html>",
    "/hidden": b"<html><title>Hidden</title>Secret. <a href='/'>Home</a></html>",
}

class MockSiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in PAGES:
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(PAGES[self.path])
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress logging to keep test output clean
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    pass


@pytest.fixture(scope="module")
def mock_server():
    """Starts a local HTTP server on an ephemeral port. Yields the base URL."""
    server = ThreadedHTTPServer(("127.0.0.1", 0), MockSiteHandler)
    port = server.server_address[1]
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    yield f"http://127.0.0.1:{port}"
    
    server.shutdown()
    server.server_close()
    thread.join()


def test_full_crawl_integration(mock_server: str) -> None:
    """Verifies that the crawler can discover all pages on a site via BFS without duplicates."""
    
    # 1. Setup
    idx = Index()
    cfg = Config(seeds=[mock_server + "/"], max_depth=3, num_workers=2, queue_cap=10, rate_per_sec=0)
    coord = Coordinator(cfg, idx)
    stop_event = threading.Event()

    # 2. Run crawl asynchronously
    crawl_thread = threading.Thread(target=coord.start, args=(stop_event,), daemon=True)
    crawl_thread.start()

    # Give crawler time to fetch 4 tiny local pages across 2 workers
    time.sleep(1.0)
    
    # 3. Shutdown
    stop_event.set()
    crawl_thread.join(timeout=2)

    # 4. Assertions
    stats = coord.stats().snapshot()
    
    # The site has 4 pages total (/, /about, /contact, /hidden)
    assert idx.size() == 4
    assert stats["processed"] == 4
    assert stats["errors"] == 0
    
    # Check that expected data was extracted
    record_home = idx.get(mock_server + "/")
    assert record_home is not None
    assert record_home.title == "Home"
    assert "Welcome." in record_home.text

    record_hidden = idx.get(mock_server + "/hidden")
    assert record_hidden is not None
    assert record_hidden.title == "Hidden"
    assert record_hidden.depth == 2  # / -> /contact -> /hidden

    # Check search engine end-to-end
    from crawler.search import SearchEngine
    se = SearchEngine(idx)
    results = se.query("test")
    assert len(results) == 1
    assert results[0].url.endswith("/about")
