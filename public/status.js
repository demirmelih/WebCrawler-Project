// public/status.js
document.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const jobId = params.get('id');

    if (!jobId) {
        document.getElementById('no-job-warning').style.display = 'block';
        return;
    }

    document.getElementById('status-dashboard').style.display = 'block';
    document.getElementById('job-id-text').textContent = jobId;

    const UI = {
        processed: document.getElementById('metric-processed'),
        queued: document.getElementById('metric-queued'),
        errors: document.getElementById('metric-errors'),
        workers: document.getElementById('metric-workers'),
        queueFill: document.getElementById('queue-fill'),
        statusBadge: document.getElementById('job-status-badge'),
        btnStop: document.getElementById('btn-stop'),
        seedText: document.getElementById('job-seed'),
        startText: document.getElementById('job-start'),
        logsContainer: document.getElementById('state-logs')
    };

    let lastLogIdx = 0;
    let autoScroll = true;

    // Detect manual scrolling to pause auto-scroll
    UI.logsContainer.addEventListener('scroll', () => {
        const isAtBottom = UI.logsContainer.scrollHeight - UI.logsContainer.scrollTop <= UI.logsContainer.clientHeight + 10;
        autoScroll = isAtBottom;
    });

    async function pollStatus() {
        try {
            // Long poll request
            const res = await fetch(`/api/job/${jobId}?last_log_idx=${lastLogIdx}`);
            
            if (res.status === 404) {
                UI.statusBadge.textContent = "NOT FOUND";
                UI.statusBadge.className = "status-badge interrupted";
                return;
            }
            if (!res.ok) {
                // Wait briefly and retry if server error
                setTimeout(pollStatus, 2000);
                return;
            }

            const data = await res.json();
            
            // Layout Data once
            if (lastLogIdx === 0) {
                UI.seedText.textContent = escapeHTML(data.cfg.seeds.join(', '));
                UI.startText.textContent = new Date(data.start_time * 1000).toLocaleString();
            }

            // Update Metrics
            const stats = data.stats;
            UI.processed.textContent = stats.processed.toLocaleString();
            UI.errors.textContent = stats.errors.toLocaleString();
            
            const qCap = data.cfg.queue_cap || 1;
            const qSize = data.queue_size || 0;
            UI.queued.textContent = `${qSize.toLocaleString()} / ${qCap.toLocaleString()}`;
            
            const fillPct = Math.min((qSize / qCap) * 100, 100);
            UI.queueFill.style.width = `${fillPct}%`;
            if (fillPct >= 100) {
                UI.queueFill.classList.add('throttled');
            } else {
                UI.queueFill.classList.remove('throttled');
            }
            
            UI.workers.textContent = `${stats.active} / ${data.cfg.workers}`;

            // Render Worker Pool
            if (stats.worker_states) {
                const workerGrid = document.getElementById('worker-pool-grid');
                if (workerGrid) {
                    workerGrid.innerHTML = '';
                    Object.keys(stats.worker_states).sort().forEach(wId => {
                        const state = stats.worker_states[wId];
                        const isFetching = state.status === 'Fetching';
                        
                        const card = document.createElement('div');
                        card.className = 'worker-card';
                        
                        const header = document.createElement('div');
                        header.className = 'worker-card-header';
                        
                        const idSpan = document.createElement('span');
                        idSpan.textContent = wId;
                        
                        const statusSpan = document.createElement('span');
                        statusSpan.textContent = state.status;
                        statusSpan.className = isFetching ? 'worker-status-fetching' : 'worker-status-idle';
                        
                        header.appendChild(idSpan);
                        header.appendChild(statusSpan);
                        
                        const urlDiv = document.createElement('div');
                        urlDiv.className = 'worker-card-url';
                        urlDiv.textContent = state.url || '-';
                        
                        card.appendChild(header);
                        card.appendChild(urlDiv);
                        workerGrid.appendChild(card);
                    });
                }
            }

            // Update Badge & Stop Button
            UI.statusBadge.textContent = data.status;
            UI.statusBadge.className = `status-badge ${data.status}`;
            
            if (data.status === 'running') {
                UI.btnStop.style.display = 'inline-block';
                UI.btnStop.disabled = false;
            } else {
                UI.btnStop.style.display = 'none';
            }

            // Append Logs
            if (data.new_logs && data.new_logs.length > 0) {
                // Clear the loading message if it's the first log
                if (lastLogIdx === 0) {
                    UI.logsContainer.innerHTML = '';
                }

                data.new_logs.forEach(msg => {
                    const div = document.createElement('div');
                    div.className = 'log-line';
                    if (msg.toLowerCase().includes('error') || msg.toLowerCase().includes('failed')) {
                        div.classList.add('error');
                    } else if (msg.toLowerCase().includes('warn')) {
                        div.classList.add('warn');
                    }
                    div.textContent = msg;
                    UI.logsContainer.appendChild(div);
                });
                
                lastLogIdx = data.log_cursor;
                
                if (autoScroll) {
                    UI.logsContainer.scrollTop = UI.logsContainer.scrollHeight;
                }
            }

            // If job still running, poll again immediately (long polling)
            // If finished, poll slowly just in case of late straggler network logs
            if (data.status === 'running') {
                pollStatus(); // fire immediately for long-polling loop
            } else {
                setTimeout(pollStatus, 3000);
            }

        } catch(e) {
            console.error("Polling error", e);
            setTimeout(pollStatus, 5000); // Retry on network failure
        }
    }

    // Stop Crawler manually
    UI.btnStop.addEventListener('click', async () => {
        try {
            UI.btnStop.disabled = true;
            UI.btnStop.textContent = "Stopping...";
            await fetch(`/api/stop/${jobId}`, {method: 'POST'});
            // The long-polling loop will naturally catch the status update
        } catch(e) {
            alert("Failed to stop crawler.");
            UI.btnStop.disabled = false;
            UI.btnStop.textContent = "Stop Job";
        }
    });

    function escapeHTML(str) {
        if (!str) return '';
        return str.replace(/[&<>'"]/g, 
            tag => ({'&': '&amp;','<': '&lt;','>': '&gt;',"'": '&#39;','"': '&quot;'}[tag])
        );
    }

    // Live Search Binding
    const searchInput = document.getElementById('live-search-input');
    const searchResults = document.getElementById('live-search-results');
    let searchTimeout = null;

    if (searchInput && searchResults) {
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            const q = e.target.value.trim();
            if (!q) {
                searchResults.innerHTML = '<div class="text-muted" style="font-style: italic; font-size: 0.875rem;">Type to search the index...</div>';
                return;
            }
            searchTimeout = setTimeout(async () => {
                try {
                    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
                    const data = await res.json();
                    
                    if (!data.results || data.results.length === 0) {
                        searchResults.innerHTML = '<div class="text-muted" style="font-size: 0.875rem;">No results found.</div>';
                        return;
                    }

                    searchResults.innerHTML = `
                    <table style="width: 100%; text-align: left; border-collapse: collapse; font-size: 0.875rem;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted);">
                                <th style="padding: 0.5rem;">Relevant URL</th>
                                <th style="padding: 0.5rem;">Origin URL</th>
                                <th style="padding: 0.5rem;">Depth (Score)</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${data.results.map(r => `
                                <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                                    <td style="padding: 0.5rem; color: var(--success); word-break: break-all;">${escapeHTML(r.url)}</td>
                                    <td style="padding: 0.5rem; word-break: break-all; color: var(--text-muted);">${escapeHTML(r.origin || '-')}</td>
                                    <td style="padding: 0.5rem;">${r.depth} <span style="opacity:0.5">(${r.score})</span></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>`;
                } catch(err) {
                    console.error("Search error", err);
                }
            }, 300); // 300ms debounce
        });
    }

    // Start polling chain
    pollStatus();
});
