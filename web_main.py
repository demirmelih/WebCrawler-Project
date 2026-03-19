"""
web_main.py
===========
Entry point for the Web Dashboard. Starts the HTTP server on port 8080
and serves the web UI alongside the crawler REST API.
"""

import argparse
import signal
import sys
import threading
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from crawler.web import CrawlerAPIHandler, GlobalState


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Web Crawler UI Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to run the web UI on")
    args = parser.parse_args()

    # Launch server
    server = ThreadedHTTPServer(("", args.port), CrawlerAPIHandler)

    print(f"[*] Web UI server started at http://localhost:{args.port}")
    print("[*] Press Ctrl+C to stop")

    def handle_sigint(*_):
        print("\n[!] Shutting down...")
        # Signal all running jobs to stop
        with GlobalState.jobs_lock:
            for job in GlobalState.jobs.values():
                if job["status"] == "running":
                    job["stop_event"].set()
                    
        # Shutdown server asynchronously so we don't block signal handler
        threading.Thread(target=server.shutdown, daemon=True).start()
        
    signal.signal(signal.SIGINT, handle_sigint)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[*] Waiting for running jobs to cleanly exit (max 5s)...")
        # Give threads a few seconds to finish up safely
        threads = []
        with GlobalState.jobs_lock:
            for job in GlobalState.jobs.values():
                if job["thread"] and job["thread"].is_alive():
                    threads.append(job["thread"])
                    
        for t in threads:
            t.join(timeout=5)
            
        print("[*] Goodbye!")

if __name__ == "__main__":
    main()
