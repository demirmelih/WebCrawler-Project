# Web Crawler & Real-Time Search Engine

A native Python (stdlib-only) concurrent web crawler and real-time search engine. This system is designed around threading and in-memory thread-safe queues/dictionaries.

## Features
- **Concurrent Indexing**: Crawls starting from a seed URL to a maximum depth *k*.
- **Uniqueness Check**: Thread-safe visited set ensuring no duplicate page fetches.
- **Native Implementation**: Strictly uses Python standard library (`urllib.request`, `html.parser`, `threading`, `queue`).
- **Live Search**: Query the in-memory index while crawling is active without race conditions.
- **Web Dashboard**: An asynchronous web UI to launch jobs, view real-time log streaming via long polling, and search the index.
- **Back-Pressure Control**: Automatically throttles queueing and request rates per domain.
- **State Persistence (Bonus)**: Option to save crawled pages to JSON Lines format for resumability.

## Running the Application

### 1. Web Dashboard (Recommended)

Starts the native HTTP server exposing the Multi-Page Application on port 8080:

```bash
python web_main.py --port 8080
```
Open your browser to `http://localhost:8080` to launch jobs and query the database.

### 2. Standalone CLI Worker

If you prefer headless command-line operation:

```bash
python main.py --seed "https://example.com" --depth 2 --workers 10 --queue-cap 500 --rate 2.0 --persist
```
*Press `Ctrl+C` to cleanly pause/interrupt the crawl and shut down threads.*

## File Architecture
* `docs/`: Product requirements, system design, and production deployment recommendations.
* `crawler/coordinator.py`: Master node managing work queue, configuration, and worker thread lifecycle.
* `crawler/worker.py`: Daemon crawler threads running the HTTP I/O and HTML parsing loop.
* `crawler/search.py`: The live query layer performing relevance scoring (`2×title_hits + 1×body_hits`).
* `crawler/index.py`: The `RLock`-protected shared index dictionary.
* `crawler/web.py`: Real-time backend API mapping UI payloads to underlying threads.
* `crawler/persistence.py`: JSONL appender for pausing and resuming progress.
