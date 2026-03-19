"""
crawler/web.py
==============
REST API and Static File server. Provides endpoints to control multiple
crawling jobs concurrently, adhering to the multi-page dashboard requirements.
"""

import json
import logging
import os
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from crawler.coordinator import Config, Coordinator
from crawler.index import Index
from crawler.search import SearchEngine

logger = logging.getLogger(__name__)

class GlobalState:
    """Global state holder for the web server to access shared components."""
    idx = Index()
    search_engine = SearchEngine(idx)
    
    # Store jobs by ID
    jobs: dict[str, dict] = {}
    job_counter = 0
    jobs_lock = threading.Lock()


class CrawlerAPIHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for serving HTML/JS/CSS and JSON REST API."""

    def _send_json(self, status_code: int, data: dict | list) -> None:
        self.send_response(status_code)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        if path == "/api/jobs":
            # Return list of all jobs history
            with GlobalState.jobs_lock:
                jobs_out = []
                for j_id, job in GlobalState.jobs.items():
                    # Update status of complete threads
                    if job["status"] == "running" and not job["thread"].is_alive():
                        job["status"] = "completed"
                        job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Job successfully completed.")

                    jobs_out.append({
                        "id": j_id,
                        "seed": ", ".join(job["cfg"].seeds),
                        "depth": job["cfg"].max_depth,
                        "workers": job["cfg"].num_workers,
                        "status": job["status"],
                        "start_ts": job["start_ts"]
                    })
            self._send_json(200, {"jobs": jobs_out})
            return

        if path.startswith("/api/job/"):
            # Ex: /api/job/1?last_log_idx=0
            job_id = path.split("/")[-1]
            last_log_idx = int(urllib.parse.parse_qs(parsed_path.query).get("last_log_idx", ["0"])[0])
            
            with GlobalState.jobs_lock:
                job = GlobalState.jobs.get(job_id)
                
            if not job:
                self._send_json(404, {"error": "Job not found"})
                return

            # If it was running but thread is dead, mark it complete
            if job["status"] == "running" and not job["thread"].is_alive():
                job["status"] = "completed"
                job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Job successfully completed.")

            stats = job["coordinator"].stats().snapshot() if job["coordinator"] else {"processed":0, "errors":0, "active":0}
            
            q = job["coordinator"].queue_ref() if job["coordinator"] else None
            queue_size = q.qsize() if q else 0

            # Dynamic logs generation comparing against last known stats
            current_processed = stats.get("processed", 0)
            last_processed = job.get("_last_processed", 0)
            
            if current_processed > last_processed:
                job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Processed {current_processed - last_processed} new pages (Total: {current_processed})")
                job["_last_processed"] = current_processed
                
            cur_errors = stats.get("errors", 0)
            last_errors = job.get("_last_errors", 0)
            if cur_errors > last_errors:
                job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {cur_errors - last_errors} new fetch/parse errors occurred.")
                job["_last_errors"] = cur_errors

            new_logs = job["logs"][last_log_idx:]

            self._send_json(200, {
                "id": job_id,
                "status": job["status"],
                "start_time": job["start_ts"],
                "cfg": {
                    "seeds": job["cfg"].seeds,
                    "queue_cap": job["cfg"].queue_cap,
                    "workers": job["cfg"].num_workers
                },
                "stats": stats,
                "queue_size": queue_size,
                "new_logs": new_logs,
                "log_cursor": len(job["logs"])
            })
            return

        if path == "/api/search":
            query = urllib.parse.parse_qs(parsed_path.query).get("q", [""])[0]
            results = GlobalState.search_engine.query(query)
            
            out = []
            for r in results:
                record = GlobalState.idx.get(r.url)
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
            path = "/crawler.html"
            
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
        except Exception:
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
                rate = data.get("rate", 2.0)
                queue_cap = data.get("queue_cap", 500)
                
                if not seeds:
                    self._send_json(400, {"error": "Missing seeds"})
                    return

                with GlobalState.jobs_lock:
                    GlobalState.job_counter += 1
                    job_id = str(GlobalState.job_counter)

                    cfg = Config(seeds=seeds, max_depth=depth, num_workers=workers, rate_per_sec=rate, queue_cap=queue_cap)
                    coord = Coordinator(cfg, GlobalState.idx)
                    stop_event = threading.Event()
                    
                    job = {
                        "id": job_id,
                        "cfg": cfg,
                        "coordinator": coord,
                        "stop_event": stop_event,
                        "thread": None,
                        "status": "running",
                        "start_ts": datetime.now().timestamp(),
                        "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] Job #{job_id} launched with {workers} workers targeting {', '.join(seeds)}."],
                        "_last_processed": 0,
                        "_last_errors": 0
                    }

                    t = threading.Thread(
                        target=coord.start, 
                        args=(stop_event,), 
                        daemon=True
                    )
                    job["thread"] = t
                    
                    GlobalState.jobs[job_id] = job
                    t.start()
                
                self._send_json(200, {"status": "started", "job_id": job_id})
                
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        if self.path.startswith("/api/stop/"):
            job_id = self.path.split("/")[-1]
            
            with GlobalState.jobs_lock:
                job = GlobalState.jobs.get(job_id)
                if job and job["status"] == "running":
                    job["stop_event"].set()
                    # Instantly mark as interrupted to fix UX lag. Thread will eventually exit gracefully.
                    job["status"] = "interrupted"
                    job["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] User requested stop. Shutting down workers gracefully...")
                    self._send_json(200, {"status": "stopping"})
                else:
                    self._send_json(400, {"error": "Job not running or not found"})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass
