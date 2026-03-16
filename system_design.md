# System Design Document
## Web Crawler & Real-Time Search Engine

**Version:** 1.1  
**Date:** 2026-03-16  
**Language:** Python 3.11+ (stdlib only)  
**Status:** Phase 2 — Architecture & Module Interfaces

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                             main.py                                  │
│  CLI entry-point: argparse, wire modules, signal handler, threads   │
└───────┬─────────────────────┬──────────────────────┬───────────────┘
        │                     │                      │
        ▼                     ▼                      ▼
┌───────────────┐   ┌──────────────────┐   ┌────────────────────┐
│   Coordinator │   │  SearchEngine    │   │    Dashboard       │
│   (Crawler)   │   │  (Searcher)      │   │    (UI / CLI)      │
└───────┬───────┘   └────────┬─────────┘   └────────┬───────────┘
        │                    │                       │
        │          ┌─────────▼──────────┐            │
        │          │   Shared Index     │◄───────────┘
        │          │  (RLock + dict)    │   (read metrics)
        │          └─────────▲──────────┘
        │                    │ index.put(record)
        ▼                    │
┌───────────────┐    ┌───────┴────────┐
│  Work Queue   │───►│  Worker Pool   │
│ queue.Queue   │    │  N Threads     │
│ (maxsize=M)   │    │  (daemon=True) │
└───────────────┘    └───────┬────────┘
        ▲                    │ urllib.request + html.parser
        │  queue.put(block)  │
        └────────────────────┘
        (back-pressure: put blocks when queue full)
```

---

## 2. Module Breakdown

The project is divided into **6 modules** (packages/files). The dependency graph flows strictly downward — no circular imports.

```
main
 ├── coordinator   (owns WorkQueue, spawns workers)
 │    └── worker  (fetches, parses, enqueues)
 ├── index        (shared PageRecord store — thread-safe)
 ├── search       (query engine — reads index)
 ├── dashboard    (metrics display — reads index + queue stats)
 └── persistence  (optional save/load to index.jsonl)
```

---

## 3. Module Interface Definitions

### 3.1 `crawler/index.py` — Shared Index

**Responsibility:** Thread-safe, in-memory storage of all crawled `PageRecord`s.

```python
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Optional

@dataclass
class PageRecord:
    url:        str
    origin_url: str          # "" for seed
    depth:      int          # 0 = seed
    title:      str
    text:       str          # visible body text
    indexed_at: datetime

class Index:
    def __init__(self):
        self._store: dict[str, PageRecord] = {}
        self._lock  = RLock()

    def put(self, record: PageRecord) -> None:
        with self._lock:
            self._store[record.url] = record

    def get(self, url: str) -> Optional[PageRecord]:
        with self._lock:
            return self._store.get(url)

    def all(self) -> list[PageRecord]:
        with self._lock:
            return list(self._store.values())   # returns a copy

    def size(self) -> int:
        with self._lock:
            return len(self._store)
```

**Concurrency contract:** Python's `threading.RLock` is re-entrant and ensures only one thread writes at a time. All four methods acquire the same lock — reads and writes are mutually exclusive, preventing torn reads.

---

### 3.2 `crawler/coordinator.py` — Crawl Coordinator

**Responsibility:** Initialises the work queue with seed URLs, owns the worker pool lifecycle, enforces depth limits, and manages the visited set.

```python
from dataclasses import dataclass, field
from threading import Lock, Event, Thread
import queue

@dataclass
class Config:
    seeds:       list[str]
    max_depth:   int   = 3
    num_workers: int   = 10
    queue_cap:   int   = 500
    rate_per_sec: float = 2.0

@dataclass
class WorkItem:
    url:    str
    origin: str
    depth:  int

class CrawlStats:
    def __init__(self):
        self._lock = Lock()
        self.processed = self.errors = self.active_workers = 0

    def increment_processed(self): ...
    def increment_errors(self):    ...
    def snapshot(self) -> dict:    ...

class VisitedSet:
    def __init__(self):
        self._lock = Lock()
        self._seen: set[str] = set()

    def try_mark(self, url: str) -> bool:
        """Returns True if url is new (first visit), False if already seen."""
        with self._lock:
            if url in self._seen:
                return False
            self._seen.add(url)
            return True

class Coordinator:
    def __init__(self, cfg: Config, idx):
        self._cfg   = cfg
        self._idx   = idx
        self._queue = queue.Queue(maxsize=cfg.queue_cap)
        self._stats = CrawlStats()
        self._visited = VisitedSet()

    def start(self, stop_event: Event) -> None:
        # Enqueue seeds, spawn N daemon worker threads, join on stop_event
        ...

    def stats(self) -> CrawlStats: ...
    def queue_ref(self) -> queue.Queue: ...
```

---

### 3.3 `crawler/worker.py` — Crawler Worker

**Responsibility:** Picks one `WorkItem` from the queue, fetches the page using native HTTP, parses links, filters by depth, writes one `PageRecord` to the index, and enqueues child URLs.

```python
from urllib.request import urlopen, Request
from urllib.parse  import urljoin, urlparse
from urllib.error  import URLError, HTTPError
from html.parser   import HTMLParser
from threading     import Event
import queue

def run_worker(
    work_q:     queue.Queue,
    visited:    VisitedSet,
    idx:        Index,
    cfg:        Config,
    stats:      CrawlStats,
    stop_event: Event,
) -> None:
    """Runs as a daemon Thread target; loops until stop_event is set."""
    while not stop_event.is_set():
        try:
            item: WorkItem = work_q.get(timeout=1)
        except queue.Empty:
            continue
        # → fetch with urllib.request (10s timeout)
        # → parse with html.HTMLParser subclass
        # → idx.put(PageRecord(...))
        # → enqueue children with work_q.put(block=True)
        ...
```

**HTTP constraint:** `urllib.request` only. No `requests`, `httpx`, or any third-party HTTP library.

**Parsing constraint:** `html.HTMLParser` subclass only. No BeautifulSoup, lxml, etc.

---

### 3.4 `crawler/search.py` — Query Engine

**Responsibility:** Accepts a query string, scans the shared index under a read-lock, scores and ranks results, returns a list of triples.

```python
from dataclasses import dataclass

@dataclass
class ResultTriple:
    url:        str
    origin_url: str
    depth:      int
    score:      int   # for display / debugging

class SearchEngine:
    def __init__(self, idx: Index):
        self._idx = idx

    def query(self, query_str: str, limit: int = 20) -> list[ResultTriple]:
        tokens  = query_str.lower().split()
        results = []
        for record in self._idx.all():   # all() holds lock internally
            title_hits = sum(t in record.title.lower() for t in tokens)
            body_hits  = sum(t in record.text.lower()  for t in tokens)
            score = 2 * title_hits + body_hits
            if score > 0:
                results.append(ResultTriple(record.url, record.origin_url,
                                            record.depth, score))
        return sorted(results, key=lambda r: (-r.score, r.depth))[:limit]
```

**Scoring formula:**
```
score = (2 × title_hits) + (1 × body_hits)

title_hits = count of query tokens found in record.title (case-insensitive)
body_hits  = count of query tokens found in record.text  (case-insensitive)
```

---

### 3.5 `crawler/dashboard.py` — Real-Time Display

**Responsibility:** Polls `coordinator.stats()` and `index.size()` every 1 second and renders a live terminal view.

```python
from threading import Event, Thread
import time, sys

class Dashboard:
    def __init__(self, coordinator, idx, search_engine, cfg): ...

    def run(self, stop_event: Event) -> None:
        """Called in its own daemon Thread. Refreshes every 1s."""
        stdin_thread = Thread(target=self._read_stdin,
                              args=(stop_event,), daemon=True)
        stdin_thread.start()
        while not stop_event.is_set():
            self._render()
            time.sleep(1)

    def _render(self) -> None:
        # clear terminal, print box with live metrics
        ...

    def _read_stdin(self, stop_event: Event) -> None:
        # reads "search <query>" lines from stdin, calls search_engine.query()
        ...
```

**Output layout (terminal):**
```
╔══════════════════════════════════════════════╗
║        WEB CRAWLER — LIVE DASHBOARD          ║
╠══════════════════════════════════════════════╣
║  Status   : [CRAWLING]  /  [THROTTLED]       ║
║  Workers  : 8 / 10 active                    ║
║  Processed: 1,234  URLs                      ║
║  Queued   : 87  /  500 capacity              ║
║  Indexed  : 1,201  pages                     ║
║  Errors   : 12                               ║
╠══════════════════════════════════════════════╣
║  > search <query>  │  Ctrl+C to stop         ║
╚══════════════════════════════════════════════╝
```

---

### 3.6 `crawler/persistence.py` — Save / Resume (Bonus)

**Responsibility:** Serialises `PageRecord`s to `index.jsonl` after each write. Reloads on startup to resume a previous crawl.

```python
import json
from dataclasses import asdict
from threading   import Lock
from pathlib     import Path

class Persistence:
    def __init__(self, filepath: str = "index.jsonl"):
        self._path = Path(filepath)
        self._lock = Lock()

    def append(self, record: PageRecord) -> None:
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), default=str) + "\n")

    def load_all(self) -> list[PageRecord]:
        if not self._path.exists():
            return []
        records = []
        with self._lock, open(self._path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(PageRecord(**json.loads(line)))
                except Exception:
                    pass   # skip malformed lines
        return records
```

---

## 4. Data Flow: End-to-End Crawl Cycle

```
Step 1 — Startup  (main.py)
  Parse argparse flags → build Config
  Create: Index(), Coordinator(cfg, idx), SearchEngine(idx), Dashboard(...)
  stop_event = threading.Event()
  If --persist: Persistence().load_all() → pre-fill idx + visited set
  signal.signal(SIGINT, lambda *_: stop_event.set())

Step 2 — Worker loop  (crawler/worker.py, N daemon Threads)
  while not stop_event.is_set():
      item = work_q.get(timeout=1)          ← blocks if empty, raises Empty if timeout
      urllib.request.urlopen(url, timeout=10)
      if status != 2xx → stats.increment_errors(), continue
      html.HTMLParser subclass extracts title, body text, hrefs
      idx.put(PageRecord(...))              ← RLock write
      persistence.append(record)           ← if --persist
      for href in links:
          abs_url = urljoin(base, href)
          if depth+1 > cfg.max_depth → skip
          if not visited.try_mark(abs_url) → skip
          work_q.put(WorkItem(...), block=True)  ← BLOCKS when queue full (back-pressure)

Step 3 — Searcher  (any time, concurrent with Step 2)
  user types "search <query>" in dashboard stdin listener
  search_engine.query(q, limit)
      idx.all()                            ← RLock read (copy)
      score each PageRecord, sort DESC score / ASC depth
      return top N ResultTriples

Step 4 — Dashboard  (crawler/dashboard.py, 1 daemon Thread)
  every 1s: coordinator.stats().snapshot() + idx.size()
  re-render terminal box
  separate stdin Thread listens for "search <query>"

Step 5 — Shutdown  (SIGINT / Ctrl+C)
  stop_event.set() → all worker threads exit their while-loop
  main thread: stop_event.wait() returns
  daemon threads auto-cleaned up by Python runtime
  crawler.log flushed automatically
```

---

## 5. Concurrency Safety Matrix

| Shared Resource | Writers | Readers | Mechanism |
|----------------|---------|---------|-----------|
| `Index._store` (dict) | `run_worker` → `idx.put()` | `search_engine.query()` → `idx.all()`, Dashboard | `threading.RLock` (in `Index`) |
| `VisitedSet._seen` (set) | `run_worker` → `visited.try_mark()` | `run_worker` → `visited.try_mark()` | `threading.Lock` (in `VisitedSet`) |
| `work_q` (Queue) | `run_worker` → `work_q.put(block=True)` | `run_worker` → `work_q.get(timeout=1)` | `queue.Queue(maxsize=M)` — thread-safe internally |
| `CrawlStats` counters | `run_worker` → `stats.increment_*()` | Dashboard → `stats.snapshot()` | `threading.Lock` (in `CrawlStats`) |
| `index.jsonl` (file) | `Persistence.append()` | `Persistence.load_all()` | `threading.Lock` (in `Persistence`) |

---

## 6. File / Directory Structure

```
WebCrawler/
├── .antigravity            ← Phase 3 coding standards & AI prompts
├── product_prd.md          ← Phase 1 output
├── system_design.md        ← Phase 2 output (this file)
├── requirements.txt        ← dev deps only (pytest, pytest-timeout)
├── main.py                 ← CLI entry-point, wires all modules
│
├── crawler/
│   ├── __init__.py
│   ├── index.py            ← PageRecord dataclass + Index (RLock-protected dict)
│   ├── coordinator.py      ← Config, WorkItem, CrawlStats, VisitedSet, Coordinator
│   ├── worker.py           ← run_worker() — urllib.request + html.HTMLParser
│   ├── search.py           ← ResultTriple dataclass + SearchEngine
│   ├── dashboard.py        ← Dashboard — terminal UI refresh + stdin listener
│   └── persistence.py      ← JSONL append/load (threading.Lock-protected)
│
└── tests/
    ├── __init__.py
    ├── test_index.py
    ├── test_coordinator.py
    ├── test_worker.py
    ├── test_search.py
    └── test_persistence.py
```

---

## 7. Key Design Decisions & Rationale

| Decision | Chosen Approach | Rationale |
|----------|----------------|-----------|
| Language | Python 3.11+ (stdlib only) | Readable, fast to iterate, I/O-bound crawling suits threading model well |
| Concurrency primitive | `queue.Queue(maxsize=M)` | Built-in back-pressure via blocking `put()`; no external deps |
| Worker model | Fixed pool of `threading.Thread` (daemon) | Prevents unbounded thread spawning; predictable resource usage |
| Index locking | `threading.RLock` | Re-entrant; prevents deadlock when same thread re-acquires; safe for concurrent reads |
| Stats counters | `threading.Lock` + plain `int` | Python has no atomic ints; Lock is the idiomatic equivalent |
| Shutdown signal | `threading.Event` (stop_event) | Threads poll `stop_event.is_set()` — clean, no forced kill |
| HTTP client | `urllib.request` | Stdlib-only per requirements; avoids `requests`, `httpx`, Scrapy |
| HTML parser | `html.HTMLParser` subclass | Stdlib-only per requirements; avoids BeautifulSoup, lxml |
| Back-pressure | `work_q.put(block=True)` in worker | Blocks naturally when queue full; no extra rate-limit logic needed at enqueue |
| Persistence format | Newline-delimited JSON (`index.jsonl`) | Append-only; human-readable; crash-safe; easy line-by-line reload |
| Relevancy heuristic | `2×title_hits + 1×body_hits` | Simple, fast, no ML needed; title is a stronger relevance signal |

---

## 8. Phase 3 Output — `.antigravity`

Phase 3 produced **`.antigravity`** (Antigravity-native standards file) which locks in:
- Banned imports (`requests`, `beautifulsoup4`, `scrapy`, etc.)
- Allowed stdlib-only libraries (`urllib.request`, `html.parser`, `threading`, `queue`)
- Module dependency rules and naming conventions
- Concurrency rules (RLock, VisitedSet lock, stop_event pattern)
- Error handling and logging standards
- 7 verbatim AI prompt templates (one per module) for Phase 4
- Implementation order and Definition of Done checklist
