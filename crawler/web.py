"""
crawler/web.py
==============
REST API and Static File server using Python's built-in http.server.
Serves the Vanilla JS frontend and provides endpoints to control the crawler.
"""

import json
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

from crawler.coordinator import Config, Coordinator
from crawler.index import Index
from crawler.search import SearchEngine

logger = logging.getLogger(__name__)

class CrawlerContext:
    """Global state holder for the web server to access crawler components."""
    idx = Index()
    search_engine = SearchEngine(idx)
    coordinator: Coordinator | None = None
    stop_event = threading.Event()
    coord_thread: threading.Thread | None = None


class CrawlerAPIHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for serving static files and JSON REST API."""

    def _send_json(self, status_code: int, data: dict | list) -> None:
        self.send_response(status_code)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        if path == "/api/stats":
            is_running = CrawlerContext.coord_thread is not None and CrawlerContext.coord_thread.is_alive()
            
            if CrawlerContext.coordinator and is_running:
                stats = CrawlerContext.coordinator.stats().snapshot()
                q = CrawlerContext.coordinator.queue_ref()
                queue_size = q.qsize() if q else 0
                queue_cap = CrawlerContext.coordinator._cfg.queue_cap
            else:
                stats = {"processed": 0, "errors": 0, "active": 0}
                queue_size = 0
                queue_cap = 500

            self._send_json(200, {
                "is_running": is_running,
                "stats": stats,
                "queue_size": queue_size,
                "queue_cap": queue_cap,
                "index_size": CrawlerContext.idx.size()
            })
            return

        if path == "/api/search":
            query = urllib.parse.parse_qs(parsed_path.query).get("q", [""])[0]
            results = CrawlerContext.search_engine.query(query)
            
            # Augment with titles
            out = []
            for r in results:
                record = CrawlerContext.idx.get(r.url)
                title = record.title if record else "Untitled"
                out.append({
                    "url": r.url,
                    "title": title,
                    "depth": r.depth,
                    "score": r.score
                })
                
            self._send_json(200, {"results": out})
            return

        # Serve static files from public/
        if path == "/":
            path = "/index.html"
            
        public_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public")
        file_path = os.path.abspath(os.path.join(public_dir, path.lstrip("/")))
        
        # Security: prevent path traversal
        if not file_path.startswith(os.path.abspath(public_dir)):
            self.send_response(403)
            self.end_headers()
            return

        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        ext = os.path.splitext(file_path)[1]
        content_types = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript"
        }
        
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-type", content_types.get(ext, "text/plain"))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/start":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            
            try:
                data = json.loads(body)
                seeds = data.get("seeds", [])
                depth = data.get("depth", 3)
                workers = data.get("workers", 10)
                
                if not seeds:
                    self._send_json(400, {"error": "Missing seeds"})
                    return

                # If already running, don't start
                if CrawlerContext.coord_thread and CrawlerContext.coord_thread.is_alive():
                    self._send_json(400, {"error": "Crawler already running"})
                    return

                # Reset stop event and start
                CrawlerContext.stop_event.clear()
                cfg = Config(seeds=seeds, max_depth=depth, num_workers=workers)
                CrawlerContext.coordinator = Coordinator(cfg, CrawlerContext.idx)
                
                CrawlerContext.coord_thread = threading.Thread(
                    target=CrawlerContext.coordinator.start, 
                    args=(CrawlerContext.stop_event,), 
                    daemon=True
                )
                CrawlerContext.coord_thread.start()
                
                self._send_json(200, {"status": "started"})
                
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        if self.path == "/api/stop":
            CrawlerContext.stop_event.set()
            self._send_json(200, {"status": "stopping"})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress logging to keep stdout clean
        pass
