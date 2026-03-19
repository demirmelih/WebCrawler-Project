// public/search.js
document.addEventListener('DOMContentLoaded', () => {
    const searchInput = document.getElementById('search-input');
    const resultsContainer = document.getElementById('results-container');
    
    let searchTimeout;

    searchInput.addEventListener('input', (e) => {
        const q = e.target.value.trim();
        clearTimeout(searchTimeout);
        
        if (!q) {
            resultsContainer.innerHTML = '<div class="empty-state">Results based on maximum hits from the filesystem will appear here.</div>';
            return;
        }
        
        searchTimeout = setTimeout(async () => {
            try {
                const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
                if (!res.ok) throw new Error("Search API error");
                const data = await res.json();
                
                if (!data.results || data.results.length === 0) {
                    resultsContainer.innerHTML = '<div class="empty-state">No hits found for your query.</div>';
                    return;
                }
                
                resultsContainer.innerHTML = data.results.map(r => `
                    <div class="result-item">
                        <div class="result-title">${escapeHTML(r.title)}</div>
                        <div class="result-url" style="word-break: break-all;">${escapeHTML(r.url)}</div>
                        <div class="result-meta">
                            <span class="meta-badge">Score: ${r.score}</span>
                            <span class="meta-badge">Depth: ${r.depth}</span>
                        </div>
                    </div>
                `).join('');
                
            } catch(e) {
                console.error("Search failed", e);
            }
        }, 300); // 300ms debounce
    });

    function escapeHTML(str) {
        if (!str) return 'Untitled';
        return str.replace(/[&<>'"]/g, 
            tag => ({'&': '&amp;','<': '&lt;','>': '&gt;',"'": '&#39;','"': '&quot;'}[tag])
        );
    }
});
