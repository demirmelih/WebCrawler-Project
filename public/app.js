const UI = {
    processed: document.getElementById('metric-processed'),
    indexed: document.getElementById('metric-indexed'),
    queued: document.getElementById('metric-queued'),
    errors: document.getElementById('metric-errors'),
    workers: document.getElementById('metric-workers'),
    queueFill: document.getElementById('queue-fill'),
    statusPulse: document.getElementById('status-pulse'),
    statusText: document.getElementById('status-text'),
    btnStart: document.getElementById('btn-start'),
    btnStop: document.getElementById('btn-stop'),
    form: document.getElementById('crawler-form'),
    searchInput: document.getElementById('search-input'),
    resultsContainer: document.getElementById('results-container')
};

let pollInterval = null;

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) return;
        const data = await res.json();
        const stats = data.stats;
        
        UI.processed.textContent = stats.processed.toLocaleString();
        UI.indexed.textContent = data.index_size.toLocaleString();
        UI.errors.textContent = stats.errors.toLocaleString();
        
        const qCap = data.queue_cap || 1;
        const qSize = data.queue_size || 0;
        UI.queued.textContent = `${qSize.toLocaleString()}`;
        
        const fillPct = Math.min((qSize / qCap) * 100, 100);
        UI.queueFill.style.width = `${fillPct}%`;
        
        if (fillPct >= 100 && qCap > 0) {
            UI.queueFill.classList.add('throttled');
            UI.statusPulse.className = 'pulse-ring throttled';
            UI.statusText.textContent = 'THROTTLED';
        } else {
            UI.queueFill.classList.remove('throttled');
            if (data.is_running) {
                UI.statusPulse.className = 'pulse-ring active';
                UI.statusText.textContent = 'CRAWLING';
            }
        }
        
        const totalWorkers = document.getElementById('workers').value || 0;
        UI.workers.textContent = `${stats.active} / ${totalWorkers}`;
        
        if (!data.is_running) {
            UI.statusPulse.className = 'pulse-ring';
            UI.statusText.textContent = 'OFFLINE';
            UI.btnStart.disabled = false;
            UI.btnStop.disabled = true;
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
        } else {
            UI.btnStart.disabled = true;
            UI.btnStop.disabled = false;
        }
    } catch(e) {
        console.error("Failed to fetch stats", e);
    }
}

// Start crawler
UI.form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const seeds = document.getElementById('seeds').value.split(',').map(s => s.trim()).filter(Boolean);
    const payload = {
        seeds: seeds,
        depth: parseInt(document.getElementById('depth').value),
        workers: parseInt(document.getElementById('workers').value)
    };
    
    try {
        const res = await fetch('/api/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) {
            const err = await res.text();
            throw new Error(err);
        }
        
        UI.btnStart.disabled = true;
        UI.btnStop.disabled = false;
        
        if (!pollInterval) {
            pollInterval = setInterval(fetchStats, 1000);
        }
        fetchStats();
    } catch(e) {
        alert("Failed to start crawler: " + e.message);
    }
});

// Stop crawler
UI.btnStop.addEventListener('click', async () => {
    try {
        await fetch('/api/stop', {method: 'POST'});
        UI.btnStop.disabled = true;
        UI.statusText.textContent = 'SHUTTING DOWN...';
        // Polling continues until is_running = false
    } catch(e) {
        alert("Failed to stop crawler.");
    }
});

// Live Search
let searchTimeout;
UI.searchInput.addEventListener('input', (e) => {
    const q = e.target.value.trim();
    clearTimeout(searchTimeout);
    
    if (!q) {
        UI.resultsContainer.innerHTML = '<div class="empty-state">Enter a query above to search the live indexed pages.</div>';
        return;
    }
    
    searchTimeout = setTimeout(async () => {
        try {
            const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
            if (!res.ok) throw new Error("Search API error");
            const data = await res.json();
            
            if (!data.results || data.results.length === 0) {
                UI.resultsContainer.innerHTML = '<div class="empty-state">No hits found for your query.</div>';
                return;
            }
            
            UI.resultsContainer.innerHTML = data.results.map(r => `
                <div class="result-item">
                    <div class="result-title">${escapeHTML(r.title)}</div>
                    <div class="result-url">${escapeHTML(r.url)}</div>
                    <div class="result-meta">
                        <span class="meta-badge">Score: ${r.score}</span>
                        <span class="meta-badge">Depth: ${r.depth}</span>
                    </div>
                </div>
            `).join('');
            
        } catch(e) {
            console.error("Search failed", e);
        }
    }, 250); // 250ms debounce
});

function escapeHTML(str) {
    if (!str) return 'Untitled';
    return str.replace(/[&<>'"]/g, 
        tag => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            "'": '&#39;',
            '"': '&quot;'
        }[tag])
    );
}

// Initial fetch
fetchStats();
