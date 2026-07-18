import React, { useState, useEffect, useCallback } from "react";
import { SkeletonCard, SkeletonDiscovered } from "./Skeleton.jsx";

// Discovered: GP indicators listed per pair — restored to the original
// look (section heading + flat bulleted list of indicator names/expressions).
export default function DiscoveredView({ apiBase, isActive = true }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const r = await fetch(`${apiBase}/api/discovered`);
      if (!r.ok) throw new Error(`API ${r.status}`);
      setData(await r.json());
      setError(null);
    } catch (e) { setError(e.message); }
    setLoading(false);
  }, [apiBase]);

  useEffect(() => {
    if (!isActive) return;
    fetchData();
    const iv = setInterval(fetchData, 30000);
    const refresh = () => { fetchData(); };
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      clearInterval(iv);
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [fetchData, isActive]);

  if (loading) return <SkeletonDiscovered />;
  if (error) return <div className="discovered"><p className="error">{error} — <button className="retry-inline" onClick={fetchData}>retry</button></p></div>;
  if (!data || data.total_indicators === 0)
    return <div className="discovered"><h2>Discovered</h2><p className="detail-muted">No discovered indicators yet. GP runs weekly or when stuck.</p></div>;

  const pairs = data.pairs || {};
  // Original order: pairs with the strongest ensemble signal first.
  const ensemble = data.ensemble || {};
  const sorted = Object.keys(pairs).sort((a, b) => {
    const sa = Math.abs(ensemble[a]?.signal || 0);
    const sb = Math.abs(ensemble[b]?.signal || 0);
    if (sa !== sb) return sb - sa;
    return a.localeCompare(b);
  });

  return (
    <section className="discovered">
      <h2>Discovered</h2>
      <p className="discovered-count">{data.total_indicators} indicators across {data.total_pairs} pairs</p>
      {sorted.map((pair) => {
        const inds = Array.isArray(pairs[pair]) ? pairs[pair] : [];
        if (!inds.length) return null;
        const ens = ensemble[pair] || {};
        const signal = ens.signal || 0;
        const sigClass = signal > 0.3 ? "bullish" : signal < -0.3 ? "bearish" : "neutral";
        return (
          <div className="discovered-pair" key={pair}>
            <div className="discovered-pair-head">
              <span className="discovered-pair-name">{pair}</span>
              <span className={`signal-badge signal-${sigClass}`}>
                {signal > 0 ? "+" : ""}{Number(signal).toFixed(3)}
              </span>
              <span className="discovered-pair-meta">{inds.length} indicators</span>
            </div>
            <ul className="gp-indicators" data-testid="gp-indicators">
              {inds.map((ind, i) => (
                <li key={i} className="gp-indicator">
                  <span className="gp-name">{ind.name || ind.expr}</span>
                  {(ind.fitness || ind.win_rate) && (
                    <span className="gp-fit">
                      fit {Number(ind.fitness || 0).toFixed(3)}
                      {ind.win_rate ? ` · wr ${Number(ind.win_rate).toFixed(2)}` : ""}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        );
      })}

      {data.degradation && Object.keys(data.degradation).length > 0 && (
        <div className="discovered-deg">
          <div className="dc-label">Degradation Tracking</div>
          <pre className="discovered-deg-json">{JSON.stringify(data.degradation, null, 2)}</pre>
        </div>
      )}
    </section>
  );
}
