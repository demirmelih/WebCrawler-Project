# Product Requirements Document
## Web Crawler & Real-Time Search Engine

**Version:** 1.1  
**Date:** 2026-03-16  
**Language:** Python 3.11+ (stdlib only)  
**Status:** Phase 1 — Approved for Design

---

## 1. Overview

### 1.1 Problem Statement
Build a functional, concurrent web crawler ("Indexer") and a real-time search engine ("Searcher") from scratch, without relying on high-level scraping libraries. The system must demonstrate architectural soundness, safe concurrency, and live observability.

### 1.2 Objectives
- Crawl the web recursively from a seed URL to a configurable depth *k*.
- Index page content and metadata in memory (with optional persistence).
- Expose a query interface that returns ranked, triple-format results.
- Allow search queries while indexing is still in progress (live indexing).
- Display a real-time dashboard showing crawler health and queue state.

### 1.3 Out of Scope
- JavaScript rendering (no headless browser; plain HTTP only).
- High-level scraping libraries (Scrapy, Beautiful Soup, Playwright, etc.).
- Distributed / multi-machine crawling.
- Authentication-gated pages.

---

## 2. Functional Specifications

### 2.1 Indexer (Crawler)

| ID | Requirement | Detail |
|----|-------------|--------|
| F-I-01 | **Seed URL input** | Accept one or more seed URLs from the user at startup via CLI or config file. |
| F-I-02 | **Recursive crawling to depth k** | BFS/DFS traversal starting at the seed; crawl every outgoing hyperlink up to depth *k* (user-configurable, default = 3). |
| F-I-03 | **Uniqueness / Visited set** | Maintain a thread-safe "Visited" set. A URL that has already been fetched must **never** be re-fetched, regardless of how many other pages link to it. |
| F-I-04 | **Link extraction** | Parse `<a href="...">` tags using `html.HTMLParser` (stdlib). Resolve relative URLs to absolute via `urllib.parse.urljoin` before enqueueing. |
| F-I-05 | **Content indexing** | For each crawled page, store: `url`, `origin_url` (the page that linked to it), `depth`, `title`, `text` (visible body text), `indexed_at`. |
| F-I-06 | **Error tolerance** | Non-2xx responses, timeouts, and malformed HTML must be logged and skipped gracefully; they must not halt the crawler. |
| F-I-07 | **Configurable concurrency** | User can set the maximum number of concurrent worker threads via `--workers N` (default = 10). |
| F-I-08 | **Back-pressure / throttling** | `queue.Queue(maxsize=M)` — when the queue is full, `put(block=True)` blocks naturally. Rate limiting (req/sec per domain) via `time.sleep()` prevents hammering hosts. |
| F-I-09 | **Language-native HTTP** | Use `urllib.request` only. Third-party HTTP clients (`requests`, `httpx`, etc.) are **not** permitted. |

### 2.2 Searcher (Query Engine)

| ID | Requirement | Detail |
|----|-------------|--------|
| F-S-01 | **Keyword query** | Accept a free-text search query string (single or multi-word). |
| F-S-02 | **Triple-format results** | Each result is a **triple**: `(relevant_url, origin_url, depth)`. Results are returned as a ranked list of such triples. |
| F-S-03 | **Relevancy ranking** | Score each indexed page using a simple heuristic (see §3.2). Return results sorted descending by score; ties broken by ascending depth. |
| F-S-04 | **Live indexing** | Search queries can be issued at any moment—including while the Indexer is actively crawling. The Searcher reads a snapshot of the current index without blocking crawler workers. |
| F-S-05 | **Thread safety** | All reads and writes to the shared index must be protected by appropriate synchronization primitives (Mutex / RWMutex / concurrent map) to prevent data races. |
| F-S-06 | **Result limit** | By default, return the top 20 results. User may override via `--limit N`. |

### 2.3 Dashboard / UI

| ID | Requirement | Detail |
|----|-------------|--------|
| F-D-01 | **Real-time display** | A terminal dashboard (or lightweight web UI) that refreshes every 1–2 seconds. |
| F-D-02 | **Indexing progress** | Show `URLs processed` vs `URLs queued`. |
| F-D-03 | **Queue depth** | Current number of URLs waiting in the work queue. |
| F-D-04 | **Back-pressure status** | Indicate whether the queue is at capacity / throttling is active (e.g., `[THROTTLED]` / `[OK]`). |
| F-D-05 | **Worker utilization** | Number of active workers vs configured max. |
| F-D-06 | **Error count** | Running count of skipped / failed URLs. |
| F-D-07 | **Interactive search** | User can submit a search query directly from the dashboard without stopping the crawler. |

---

## 3. Technical Specifications

### 3.1 Concurrency Model

```
┌─────────────────────────────────────────────────────┐
│                      Coordinator                    │
│  (initialises queue with seed URLs, depth=0)        │
└──────────────┬──────────────────────────────────────┘
               │ spawns
       ┌───────▼────────┐
       │  Worker Pool   │  N daemon Threads
       │  (bounded, N)  │
       └───────┬────────┘
               │ fetch → parse → enqueue children
               ▼
       ┌───────────────┐       ┌──────────────────┐
       │  Work Queue   │◄──────│  Back-pressure   │
       │  (bounded, M) │       │  Controller      │
       └───────┬───────┘       └──────────────────┘
               │ write (under lock / via channel)
               ▼
       ┌───────────────┐
       │  Shared Index │  RWMutex-protected map[url → PageRecord]
       └───────┬───────┘
               │ read (concurrent with crawler writes)
               ▼
       ┌───────────────┐
       │   Searcher    │  Acquires read-lock, scans index, ranks results
       └───────────────┘
```

**Language:** Python 3.11+ — `threading` + `queue.Queue` maps 1:1 with this design. The GIL is not a constraint because crawling is entirely I/O-bound; threads release the GIL during every `urllib` network call.

### 3.2 Data Structures

#### PageRecord (in-memory index entry)
```python
@dataclass
class PageRecord:
    url:        str
    origin_url: str      # parent URL ("" for seed)
    depth:      int      # 0 = seed
    title:      str
    text:       str      # visible body text
    indexed_at: datetime
```

#### Visited Set
`threading.Lock` + `set[str]` — `try_mark(url)` is atomic (check-and-insert under one lock acquisition).

#### Work Queue
`queue.Queue(maxsize=M)` — thread-safe; `put(block=True)` provides natural back-pressure.

#### Shared Index
`threading.RLock` + `dict[str, PageRecord]` — all reads and writes go through `Index` class methods; lock held only during dict access.

### 3.3 Back-Pressure Strategy

Two complementary mechanisms:

1. **Bounded Queue** — `queue.Queue(maxsize=M)` (default M = 500). When a worker calls `work_q.put(block=True)` and the queue is full, the call **blocks** until a slot is free. Natural back-pressure with no extra code.

2. **Per-Domain Rate Limiter** — Each domain is allowed at most *R* requests per second (default R = 2). A `time.sleep()` gate inside the worker enforces compliance, preventing hammering a single host.

### 3.4 Relevancy Heuristic

Score for a query *Q* against a PageRecord *P*:

```python
tokens     = query_str.lower().split()
title_hits = sum(t in record.title.lower() for t in tokens)
body_hits  = sum(t in record.text.lower()  for t in tokens)
score      = 2 * title_hits + body_hits
```

Results sorted: `score DESC, depth ASC`. Ties broken by proximity to seed.

### 3.5 In-Memory vs. Persistent Storage

| Mode | When to use | How |
|------|-------------|-----|
| **In-Memory (primary)** | Default; fastest; data lost on exit. | `map` / `dict` in process memory. |
| **Persistent (bonus)** | When `--persist` flag is set. | Append crawl records as newline-delimited JSON to `index.jsonl`. At startup, reload existing records from this file to resume an interrupted crawl. |

Resume logic:
1. On startup with `--persist`, read `index.jsonl` → re-populate visited set and index.
2. Re-enqueue seed URLs at depth 0; uniqueness check ensures already-visited URLs are skipped.
3. Crawl continues from where it left off.

---

## 4. API / CLI Interface

```
Usage:
  python main.py [flags]

Arguments (argparse):
  --seed      str    Seed URL(s) — required, repeatable
  --depth     int    Maximum crawl depth k (default 3)
  --workers   int    Concurrent worker thread count (default 10)
  --queue-cap int    Work queue capacity M (default 500)
  --rate      float  Max requests/sec per domain (default 2.0)
  --persist          Enable persistent index (writes index.jsonl)
  --limit     int    Max search results to return (default 20)
```

### Search Interface (interactive CLI)
```
> search <query terms>
→ Returns ranked list of triples:

  #1  url: https://example.com/page
      origin: https://example.com/
      depth: 1   score: 7

  #2  url: https://example.com/docs
      origin: https://example.com/page
      depth: 2   score: 4
```

---

## 5. Architecture: Concurrent Indexing + Live Search

The key design decision is a **shared-memory, `threading.RLock`** model:

```
┌────────────────────────────────────────────────────────────────────┐
│                         Shared Index                               │
│  threading.RLock (inside Index class)                              │
│                                                                    │
│  run_worker  → idx.put(record)  → with self._lock: dict[url]=rec  │
│  SearchEngine → idx.all()       → with self._lock: list(dict)     │
└────────────────────────────────────────────────────────────────────┘
```

- **Writers (`run_worker`)** hold the lock only for the brief `dict` insertion. Minimal contention.
- **Readers (`SearchEngine.query`)** call `idx.all()` which acquires the lock briefly to copy dict values into a list, then releases it. Scoring happens on the **copy** outside the lock — zero blocking of workers during scoring.
- The searcher never sees a torn write because the lock is released only after the record is fully inserted.
- **Eventual consistency:** a search at time *t* returns all pages whose lock was released before `idx.all()` acquired it.

---

## 6. Success Metrics

### 6.1 Performance
| Metric | Target |
|--------|--------|
| Crawl throughput | ≥ 50 URLs/min with N=10 workers on a public site |
| Search latency | < 200 ms for a full index scan of 10,000 records |
| Queue back-pressure onset | ≤ 5% worker idle time due to blocking |
| Memory usage | < 500 MB for 50,000 indexed pages |

### 6.2 Correctness
| Metric | Target |
|--------|--------|
| Duplicate pages | 0 — `VisitedSet.try_mark()` is atomic under `threading.Lock` |
| Data races | 0 — verified by concurrent thread tests in `pytest tests/ -v` |
| Max depth respected | No page crawled at depth > k |

### 6.3 System Visibility
| Metric | Target |
|--------|--------|
| Dashboard refresh rate | ≤ 2 seconds |
| Metrics accuracy | Queue depth and processed count accurate within ±1 URL |
| Throttle indicator | Correct `[THROTTLED]` / `[OK]` status shown in real time |

---

## 7. Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| **Language** | Python 3.11+ stdlib only; no Scrapy, BeautifulSoup, requests, httpx, lxml |
| **Portability** | `python main.py` runs on Linux, macOS, and Windows without OS-specific deps |
| **Observability** | JSON log lines written to `crawler.log`; human-readable level to stderr |
| **Graceful shutdown** | `Ctrl+C` → `stop_event.set()` → all daemon threads exit cleanly, no traceback |
| **Testing** | `pytest tests/ -v` covers: visited-set uniqueness, queue back-pressure, relevancy scoring, triple format |

---

## 8. Phase Delivery Plan

| Phase | Deliverable | Owner |
|-------|-------------|-------|
| **Phase 1 – PRD** | `product_prd.md` (this document) | Architect |
| **Phase 2 – Design** | System architecture diagram, module interface definitions | Architect + AI |
| **Phase 3 – Prompting** | `.antigravity` coding standards, AI prompt templates | Architect |
| **Phase 4 – Implementation** | Core crawler → Search engine → Dashboard | AI (supervised) |

---

## 9. Glossary

| Term | Definition |
|------|------------|
| **Depth k** | Number of hops from the seed URL; seed itself is depth 0 |
| **Triple** | A result record `(relevant_url, origin_url, depth)` |
| **Back-pressure** | Mechanism by which downstream capacity limits upstream throughput |
| **Visited set** | A deduplicated set of all URLs ever enqueued for crawling |
| **RWMutex** | Reader-Writer Mutex: multiple concurrent readers OR one exclusive writer |
| **Worker pool** | A fixed number of goroutines/threads that consume URLs from a shared queue |
