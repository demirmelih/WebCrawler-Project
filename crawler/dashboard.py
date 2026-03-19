"""
crawler/dashboard.py
====================
Live terminal UI for the Web Crawler.

Refreshes every 1 second printing crawl statistics and reading stdin
for search queries using a background thread.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crawler.coordinator import Config, Coordinator
    from crawler.index import Index
    from crawler.search import ResultTriple, SearchEngine


class Dashboard:
    """Live terminal UI displaying crawl statistics and handling search queries."""

    def __init__(
        self,
        coordinator: "Coordinator",
        idx: "Index",
        search_engine: "SearchEngine",
        cfg: "Config",
    ) -> None:
        self._coord = coordinator
        self._idx = idx
        self._search = search_engine
        self._cfg = cfg

        self._last_query: str = ""
        self._last_results: list[ResultTriple] = []

    def _input_worker(self, stop_event: threading.Event) -> None:  # pragma: no cover
        """Daemon thread reading stdin line-by-line for search queries.
        
        Note: sys.stdin.readline() blocks, which is why it must be in a daemon
        thread. When the main program exits, this thread will simply be killed.
        """
        while not stop_event.is_set():
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                line = line.strip()
                if line.startswith("search "):
                    query = line[7:].strip()
                    if query:
                        self._last_query = query
                        self._last_results = self._search.query(query)
            except Exception:
                pass

    def run(self, stop_event: threading.Event) -> None:  # pragma: no cover
        """Dashboard refresh loop running in its own thread."""
        input_thread = threading.Thread(
            target=self._input_worker, args=(stop_event,), daemon=True
        )
        input_thread.start()

        while not stop_event.is_set():
            self._render()
            stop_event.wait(1.0)

    def _render(self) -> None:
        """Prints the ASCII UI to stdout."""
        stats = self._coord.stats().snapshot()
        
        # Check queue size (if coordinator started)
        q = self._coord.queue_ref()
        queued = q.qsize() if q else 0
        cap = self._cfg.queue_cap

        status = "[THROTTLED]" if queued >= cap else "[CRAWLING]"

        # ANSI clear screen + reset cursor home
        print("\033[2J\033[H", end="")

        print("╔══════════════════════════════════════════════╗")
        print("║        WEB CRAWLER — LIVE DASHBOARD          ║")
        print("╠══════════════════════════════════════════════╣")
        print(f"║  Status   : {status:<32} ║")

        worker_str = f"{stats['active']} / {self._cfg.num_workers} active"
        print(f"║  Workers  : {worker_str:<32} ║")

        print(f"║  Processed: {stats['processed']:<32} ║")

        queue_str = f"{queued}  /  {cap} capacity"
        print(f"║  Queued   : {queue_str:<32} ║")

        print(f"║  Indexed  : {self._idx.size():<32} ║")
        print(f"║  Errors   : {stats['errors']:<32} ║")
        print("╠══════════════════════════════════════════════╣")
        print("║  > search <query>  │  Ctrl+C to stop         ║")
        print("╚══════════════════════════════════════════════╝")

        if self._last_query:
            print(f"\nSearch results for '{self._last_query}':")
            if not self._last_results:
                print("  (No results found)")
            else:
                for idx, res in enumerate(self._last_results, 1):
                    # Fetch live title if available
                    record = self._idx.get(res.url)
                    title = record.title if record else "Unknown Title"
                    print(f"\n  {idx}. {title}")
                    print(f"     URL  : {res.url}")
                    print(f"     Score: {res.score}  |  Depth: {res.depth}")

        sys.stdout.flush()
