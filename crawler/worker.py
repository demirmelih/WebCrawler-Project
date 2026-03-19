"""
crawler/worker.py
=================
Crawler worker — fetches URLs, parses HTML, writes to Index, enqueues children.

Role in the system
------------------
This file defines `run_worker()`, which is executed by `Coordinator` in multiple
daemon threads. It endlessly consumes `WorkItem`s from the queue until `stop_event`
is set.

It performs the actual networking and HTML parsing using strictly stdlib components:
  - `urllib.request` for fetching HTTP data (up to 2MB).
  - `html.HTMLParser` for extracting the title, body text, and outgoing links.
  - Rate limiting per domain is enforced by tracking timestamps across all threads.

Thread Safety
-------------
- `urllib.request` is inherently thread-safe.
- Interacts with shared `VisitedSet` and `CrawlStats` (which manage their own locks).
- Writes to `Index`, which manages its own `RLock`.
- Domain rate limiting uses a central `threading.Lock`.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from crawler.coordinator import WorkItem
from crawler.index import PageRecord

if TYPE_CHECKING:
    from crawler.coordinator import Config, CrawlStats, VisitedSet
    from crawler.index import Index

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate Limiting (Per-Domain across all workers)
# ---------------------------------------------------------------------------

_domain_last_fetch: dict[str, float] = {}
_rl_lock = threading.Lock()


def _enforce_rate_limit(domain: str, max_rate: float) -> None:
    """Sleep if necessary to ensure max_rate is not exceeded for the given domain."""
    if max_rate <= 0:
        return

    delay = 1.0 / max_rate

    with _rl_lock:
        now = time.time()
        last = _domain_last_fetch.get(domain, 0.0)
        elapsed = now - last

        sleep_time = 0.0
        if elapsed < delay:
            sleep_time = delay - elapsed
            _domain_last_fetch[domain] = now + sleep_time
        else:
            _domain_last_fetch[domain] = now

    if sleep_time > 0:
        time.sleep(sleep_time)


# ---------------------------------------------------------------------------
# HTML Parser
# ---------------------------------------------------------------------------

class PageParser(HTMLParser):
    """Event-driven HTML parser to extract:
      1. <title> text
      2. visible body text (ignores scripts, styles)
      3. <a href="..."> links
    """

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []

        self._in_title = False
        # Do not extract text from inside these tags
        self._ignore_tags = {"script", "style", "noscript", "meta", "head"}
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag in self._ignore_tags:
            self._ignore_depth += 1
        elif tag == "a":
            for attr, val in attrs:
                if attr == "href" and val:
                    self.links.append(val)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag in self._ignore_tags:
            self._ignore_depth = max(0, self._ignore_depth - 1)

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return

        if self._in_title:
            self.title_parts.append(text)

        # Append to body text as long as we aren't inside a <script> or <style>
        if self._ignore_depth == 0:
            self.text_parts.append(text)


# ---------------------------------------------------------------------------
# Worker Thread Logic
# ---------------------------------------------------------------------------

def run_worker(
    work_q:     "queue.Queue[WorkItem]",
    visited:    "VisitedSet",
    idx:        "Index",
    cfg:        "Config",
    stats:      "CrawlStats",
    stop_event: threading.Event,
) -> None:
    """Endless loop that fetches URLs and parses them until shutdown."""
    while not stop_event.is_set():
        try:
            item = work_q.get(timeout=1.0)
        except queue.Empty:
            continue

        stats.increment_active()
        try:
            # 1. Rate Limit
            domain = urllib.parse.urlparse(item.url).netloc
            _enforce_rate_limit(domain, cfg.rate_per_sec)

            # 2. Fetch
            req = urllib.request.Request(
                item.url,
                headers={"User-Agent": "AntigravityBot/1.0"}
            )

            with urllib.request.urlopen(req, timeout=10.0) as resp:
                # We only parse HTML
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type.lower():
                    logger.debug("Skipping non-HTML URL: %s", item.url)
                    continue

                # Cap read at 2MB to prevent memory exhaustion
                raw_bytes = resp.read(2_000_000)

            # 3. Parse
            html_text = raw_bytes.decode(errors="replace")
            parser = PageParser()
            parser.feed(html_text)

            title = " ".join(parser.title_parts).strip()
            text  = " ".join(parser.text_parts).strip()

            # 4. Write to Index
            idx.put(PageRecord(
                url=item.url,
                origin_url=item.origin,
                depth=item.depth,
                title=title,
                text=text,
                indexed_at=datetime.now(timezone.utc),
            ))

            stats.increment_processed()

            # 5. Enqueue Discovered Links
            if item.depth + 1 <= cfg.max_depth:
                for target_href in parser.links:
                    # Resolve to absolute URL based on the current page's URL
                    abs_url = urllib.parse.urljoin(item.url, target_href)

                    # Strip URL fragment (#anchor) for deduplication
                    abs_url, _ = urllib.parse.urldefrag(abs_url)

                    # Only follow common web schemes
                    if not abs_url.startswith(("http://", "https://")):
                        continue

                    # Attempt to enqueue if not visited
                    if visited.try_mark(abs_url):
                        try:
                            new_item = WorkItem(
                                url=abs_url,
                                origin=item.url,
                                depth=item.depth + 1
                            )
                            work_q.put(new_item, block=True, timeout=5.0)
                        except queue.Full:
                            logger.warning("Queue full, dropping link %s", abs_url)

        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            stats.increment_errors()
            logger.warning("Failed fetch %s: %s", item.url, e)
        except Exception as e:
            stats.increment_errors()
            logger.warning("Failed to process %s: %s", item.url, e)
        finally:
            stats.decrement_active()
            work_q.task_done()
