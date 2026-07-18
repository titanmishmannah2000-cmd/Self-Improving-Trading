import React, { useState, useEffect, useCallback } from "react";
import { SkeletonCard, SkeletonDiscovered } from "./Skeleton.jsx";

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

  useEffect(() => { if (!isActive) return; fetchData(); const iv = setInterval(fetchData, 30000); const refresh = () => { fetchData(); }; document.addEventListener("visibilitychange", refresh); window.addEventListener("focus", refresh); return () => { clearInterval(iv); document.removeEventListener("visibilitychange", refresh); window.removeEventListener("focus", refresh); }; }, [fetchData, isActive]);

  if (loading) return <SkeletonDiscovered />;
  if (error) return <div className="reports"><p className="error">{error} — <button className="retry-inline" onClick={fetchData}>retry</button></p></div>;
  if (!data || data.total_indicators === 0)
    return <div className="reports"><p>No discovered indicators yet. GP runs weekly or when stuck.</p></div>;

  const pairs = data.pairs || {};
  const ensemble = data.ensemble || {};
  // Sort by ensemble signal strength if available, else by pair name.
  const sorted = Object.keys(pairs).sort((a, b) => {
    const sa = Math.abs(ensemble[b]?.signal || 0);
    const sb = Math.abs(ensemble[a]?.signal || 0);
    if (sa !== sb) return sa - sb;
    return a.localeCompare(b);
  });

  return (
    <div className="reports">
      <div className="reports-head">
        <div className="reports-title">Discovered Indicators</div>
        <div className="reports-subtitle">{data.total_indicators} indicators across {data.total_pairs} pairs</div>
      </div>

      {sorted.map((pair) => {
        const inds = Array.isArray(pairs[pair]) ? pairs[pair] : [];
        const ens = ensemble[pair] || {};
        const signal = ens.signal || 0;
        const sigClass = signal > 0.3 ? "bullish" : signal < -0.3 ? "bearish" : "neutral";
        const sigIcon = signal > 0.3 ? "🟢" : signal < -0.3 ? "🔴" : "⚪";
        return (
          <div key={pair} className="report-card" style={{ marginBottom: 16 }}>
            <div className="report-card-header">
              <span className="report-pair">{pair}</span>
              <span className={`signal-badge signal-${sigClass}`}>
                {sigIcon} {signal > 0 ? "+" : ""}{Number(signal).toFixed(3)}
              </span>
              <span style={{ marginLeft: 12, opacity: 0.7 }}>
                {inds.length} indicators ({ens.multi_dim || 0} cross-asset)
              </span>
            </div>
            <div className="perf-stats" style={{ marginTop: 8 }}>
              <div className="perf-stat"><label>Best Fitness</label><span>{(ens.best_fitness || 0).toFixed(2)}</span></div>
              <div className="perf-stat"><label>Best OOS Corr</label><span>{(ens.best_wr || 0).toFixed(2)}</span></div>
            </div>
            {inds.length > 0 && (
              <table className="trade-table" style={{ marginTop: 8, fontSize: "0.85em" }}>
                <thead>
                  <tr><th>Expression</th><th>Fit</th><th>OOS Corr</th><th>Complexity</th></tr>
                </thead>
                <tbody>
                  {inds.map((ind, i) => (
                    <tr key={i}>
                      <td style={{ fontFamily: "monospace", fontSize: "0.8em", maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis" }}
                          title={ind.expr}>{ind.expr}</td>
                      <td>{(ind.fitness || 0).toFixed(3)}</td>
                      <td>{(ind.oos_corr || 0).toFixed(3)}</td>
                      <td>{ind.complexity ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}

      {data.degradation && Object.keys(data.degradation).length > 0 && (
        <div className="report-card" style={{ marginTop: 16 }}>
          <div className="report-card-header">Degradation Tracking</div>
          <pre style={{ fontSize: "0.8em", whiteSpace: "pre-wrap" }}>{JSON.stringify(data.degradation, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
