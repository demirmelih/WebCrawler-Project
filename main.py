"""
main.py
=======
Entry point for the Web Crawler. Wires all components together, parses CLI flags,
sets up logging, and handles OS signals for graceful shutdown.
"""

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import datetime

from crawler.coordinator import Config, Coordinator
from crawler.dashboard import Dashboard
from crawler.index import Index
from crawler.persistence import Persistence
from crawler.search import SearchEngine


class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for crawler.log."""
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "level": record.levelname,
            "ts": datetime.utcnow().isoformat() + "Z",
            "message": record.getMessage(),
        }
        if hasattr(record, "url"):
            log_data["url"] = getattr(record, "url")
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


def configure_logging() -> None:
    """Setup crawler.log (DEBUG JSON). We omit stderr handler because the Dashboard
    takes over the terminal with clear-screen renders."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler("crawler.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    root.addHandler(file_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Web Crawler & Real-Time Search Engine")
    parser.add_argument("--seed", type=str, required=True, nargs="+", help="Seed URL(s) to start crawling")
    parser.add_argument("--depth", type=int, default=3, help="Max crawl depth from seed (default: 3)")
    parser.add_argument("--workers", type=int, default=10, help="Number of concurrent workers (default: 10)")
    parser.add_argument("--queue-cap", type=int, default=500, help="Max queue capacity (default: 500)")
    parser.add_argument("--rate", type=float, default=2.0, help="Max requests/sec per domain (default: 2.0)")
    parser.add_argument("--persist", action="store_true", help="Enable JSONL persistence (resume capability)")
    parser.add_argument("--limit", type=int, default=20, help="Max search results to return (default: 20)")

    args = parser.parse_args()

    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting crawler", extra={"seeds": args.seed})

    # 1. Core instances
    idx = Index()
    cfg = Config(
        seeds=args.seed,
        max_depth=args.depth,
        num_workers=args.workers,
        queue_cap=args.queue_cap,
        rate_per_sec=args.rate
    )
    
    # 2. Persistence / Resume
    if args.persist:
        store = Persistence()
        # Resume from disk
        records = store.load_all()
        for r in records:
            idx.put(r)
            
        logger.info("Resumed %d records from index.jsonl", len(records))

        # Hook idx.put to automatically append to disk whenever a worker saves a page.
        # This keeps worker.py ignorant of persistence without breaking the design.
        original_put = idx.put
        def persisting_put(record):
            original_put(record)
            store.append(record)
        idx.put = persisting_put

    # 3. Wiring
    coordinator = Coordinator(cfg, idx)
    search_engine = SearchEngine(idx)
    dashboard = Dashboard(coordinator, idx, search_engine, cfg)
    
    # Ensure VisitedSet knows about loaded records so we don't re-crawl them at depth 0
    if args.persist and records:
        for r in records:
            coordinator._visited.try_mark(r.url)

    stop_event = threading.Event()

    # 4. Signal handler for graceful shutdown (Ctrl+C)
    def handle_sigint(*_):
        stop_event.set()
        # Terminal gets messy after ^C and the dashboard; add a clean newline.
        print("\n[!] Shutting down gracefully... Please wait for workers to finish.")
        
    signal.signal(signal.SIGINT, handle_sigint)

    # 5. Launch threads
    logger.info("Launching threads...")
    
    # Coordinator starts workers and blocks inside its thread until stop_event is set
    coord_thread = threading.Thread(target=coordinator.start, args=(stop_event,), daemon=True)
    coord_thread.start()
    
    # Dashboard loop
    dash_thread = threading.Thread(target=dashboard.run, args=(stop_event,), daemon=True)
    dash_thread.start()

    # 6. Block main thread until shutdown
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        # Fallback if signal handler gets bypassed
        stop_event.set()

    # Wait cleanly for threads to join
    coord_thread.join(timeout=10)
    logger.info("Crawler fully shut down.")


if __name__ == "__main__":
    main()
