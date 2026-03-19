// public/crawler.js
document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('crawler-form');
    const btnStart = document.getElementById('btn-start');
    const jobsList = document.getElementById('jobs-list');

    // Fetch existing jobs
    async function loadJobs() {
        try {
            const res = await fetch('/api/jobs');
            if (!res.ok) return;
            const data = await res.json();
            
            if (data.jobs.length === 0) {
                jobsList.innerHTML = '<div class="empty-state">No previous crawler operations found.</div>';
                return;
            }

            // Sort by start_time descending
            const sorted = data.jobs.sort((a,b) => b.start_ts - a.start_ts);
            
            jobsList.innerHTML = sorted.map(job => `
                <div class="job-row">
                    <div>
                        <div style="font-weight: 600; margin-bottom: 0.25rem;">
                            <a href="/status.html?id=${job.id}" class="accent" style="text-decoration: none;">Job #${job.id}</a>
                        </div>
                        <div style="font-size: 0.85rem; color: var(--text-muted);">
                            Seed: ${escapeHTML(job.seed)} | Config: Depth ${job.depth}, Workers: ${job.workers}
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 1.5rem;">
                        <span class="status-badge ${job.status}">${job.status}</span>
                        <a href="/status.html?id=${job.id}" class="btn primary" style="padding: 0.5rem 1rem; font-size: 0.85rem; text-decoration: none;">View</a>
                    </div>
                </div>
            `).join('');

        } catch(e) {
            console.error("Failed to load jobs", e);
            jobsList.innerHTML = '<div class="empty-state" style="color: var(--danger);">Failed to load connection to server.</div>';
        }
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        btnStart.disabled = true;
        btnStart.textContent = "Launching...";

        const seeds = document.getElementById('seeds').value.split(',').map(s => s.trim()).filter(Boolean);
        const payload = {
            seeds: seeds,
            depth: parseInt(document.getElementById('depth').value),
            workers: parseInt(document.getElementById('workers').value),
            rate: parseFloat(document.getElementById('rate').value),
            queue_cap: parseInt(document.getElementById('queue-cap').value)
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
            const data = await res.json();
            
            // Redirect straight to status page
            window.location.href = `/status.html?id=${data.job_id}`;
        } catch(e) {
            alert("Failed to start crawler: " + e.message);
            btnStart.disabled = false;
            btnStart.textContent = "Start Crawling";
        }
    });

    function escapeHTML(str) {
        if (!str) return '';
        return str.replace(/[&<>'"]/g, 
            tag => ({'&': '&amp;','<': '&lt;','>': '&gt;',"'": '&#39;','"': '&quot;'}[tag])
        );
    }

    loadJobs();
    // Poll job list every 5 seconds just to keep statuses fresh
    setInterval(loadJobs, 5000);
});
