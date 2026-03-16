# Product Requirements Document
## Web Crawler & Real-Time Search Engine

**Version:** 1.0  
**Date:** 2026-03-16  
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
| F-I-04 | **Link extraction** | Parse `<a href="...">` tags using language-native HTML parsing (Python `html.parser` / Go `golang.org/x/net/html`). Resolve relative URLs to absolute form before enqueueing. |
| F-I-05 | **Content indexing** | For each crawled page, store: `url`, `origin_url` (the page that linked to it), `depth`, `title`, `raw_text` (visible body text), `timestamp`. |
| F-I-06 | **Error tolerance** | Non-2xx responses, timeouts, and malformed HTML must be logged and skipped gracefully; they must not halt the crawler. |
| F-I-07 | **Configurable concurrency** | User can set the maximum number of concurrent workers (goroutines / threads) via CLI flag `--workers N` (default = 10). |
| F-I-08 | **Back-pressure / throttling** | The work queue (channel / bounded queue) must have a configurable maximum depth. When the queue is full, producers block rather than spawning unbounded goroutines. Rate limiting (requests/second per domain) is enforced to avoid hammering hosts. |
| F-I-09 | **Language-native HTTP** | Use `net/http` (Go) or `urllib` (Python) only. Third-party HTTP clients (requests, httpx, etc.) are **not** permitted. |

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
       │  Worker Pool   │  N goroutines / threads
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

**Language recommendation:** Go — channels and goroutines are a natural fit. Python with `threading` + `queue.Queue` is an acceptable alternative.

### 3.2 Data Structures

#### PageRecord (in-memory index entry)
```
PageRecord {
    url        string    // normalized absolute URL
    origin_url string    // parent URL that discovered this page
    depth      int       // crawl depth from seed (0 = seed)
    title      string    // <title> tag content
    text       string    // visible body text (whitespace-collapsed)
    indexed_at time.Time // UTC timestamp of crawl
}
```

#### Visited Set
- **Go:** `sync.Map` or `map[string]bool` protected by `sync.Mutex`.
- **Python:** `set` protected by `threading.Lock`.

#### Work Queue
- **Go:** Buffered channel `chan WorkItem` with capacity *M*.
- **Python:** `queue.Queue(maxsize=M)`.

#### Shared Index
- **Go:** `sync.RWMutex` + `map[string]PageRecord` — readers use `RLock()`, writers use `Lock()`.
- **Python:** `threading.RLock` + `dict`.

### 3.3 Back-Pressure Strategy

Two complementary mechanisms:

1. **Bounded Queue** — The work queue has a fixed maximum size *M* (default = 500). When a worker tries to enqueue a newly-discovered URL and the queue is full, the enqueue operation **blocks** (channel send / `Queue.put(block=True)`) until a slot becomes free. This provides natural back-pressure without busy-waiting.

2. **Per-Domain Rate Limiter** — Each domain is allowed at most *R* requests per second (default R = 2). A `time.Ticker` (Go) or `threading.Event` + `time.sleep` (Python) gate ensures compliance. This prevents hammering a single host and getting IP-blocked.

### 3.4 Relevancy Heuristic

Score for a query *Q* against a PageRecord *P*:

```
score(P, Q) = (2 × title_hits) + (1 × body_hits)

where:
  title_hits = count of query tokens found in P.title (case-insensitive)
  body_hits  = count of query tokens found in P.text  (case-insensitive)
```

Results are sorted: `score DESC, depth ASC`. Pages with the same score are ranked by how close they are to the seed.

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
  crawler [flags]

Flags:
  --seed      string   Seed URL to start crawling (required)
  --depth     int      Maximum crawl depth k (default 3)
  --workers   int      Concurrent worker count (default 10)
  --queue-cap int      Work queue capacity M (default 500)
  --rate      float    Max requests/sec per domain (default 2.0)
  --persist           Enable persistent index (writes index.jsonl)
  --limit     int      Max search results to return (default 20)
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

The key design decision is a **shared-memory, reader-writer lock** model:

```
┌────────────────────────────────────────────────────────────────────┐
│                         Shared Index                               │
│  sync.RWMutex (Go) / threading.RLock (Python)                      │
│                                                                    │
│  Crawler workers → Lock() → write PageRecord → Unlock()            │
│  Searcher        → RLock() → read index snapshot → RUnlock()       │
└────────────────────────────────────────────────────────────────────┘
```

- **Writers (crawler workers)** acquire an exclusive write lock only for the brief moment it takes to insert one `PageRecord`. This keeps lock contention minimal.
- **Readers (searcher)** acquire a shared read lock, allowing multiple concurrent searches without blocking each other.
- The searcher never sees a torn write (partially-written record) because the write lock is released only after the entire record is inserted.
- This architecture provides **eventual consistency**: a search issued at time *t* returns all pages indexed before *t*, plus any pages whose write lock was released before the `RLock` was acquired.

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
| Duplicate pages | 0 (visited set must be leak-free) |
| Data races | 0 detected under `-race` (Go) or `threading` sanitizer |
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
| **Language constraints** | Use only language-native HTTP and HTML parsing; no Scrapy, Beautiful Soup, requests, etc. |
| **Portability** | Must run on Linux, macOS, and Windows without OS-specific dependencies |
| **Observability** | Structured log lines (JSON preferred) written to `crawler.log` for post-mortem analysis |
| **Graceful shutdown** | `Ctrl+C` / SIGINT triggers a clean drain: workers finish current page, queue is flushed to disk if `--persist` is set |
| **Testing** | Unit tests for: visited-set uniqueness, back-pressure queue blocking, relevancy scoring, triple serialization |

---

## 8. Phase Delivery Plan

| Phase | Deliverable | Owner |
|-------|-------------|-------|
| **Phase 1 – PRD** | `product_prd.md` (this document) | Architect |
| **Phase 2 – Design** | System architecture diagram, module interface definitions | Architect + AI |
| **Phase 3 – Prompting** | `.cursorrules` coding standards, AI prompt templates | Architect |
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
