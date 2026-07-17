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

  const sorted = Object.entries(data.ensemble || {}).sort((a, b) => Math.abs(b[1]?.signal||0) - Math.abs(a[1]?.signal||0));

  return (
    <div className="reports">
      <div className="reports-head">
        <div className="reports-title">Discovered Indicators</div>
        <div className="reports-subtitle">{data.total_indicators} indicators across {data.total_pairs} pairs</div>
      </div>

      {sorted.map(([pair, ens]) => {
        const inds = data.pairs?.[pair] || [];
        const sigClass = ens.signal > 0.3 ? "bullish" : ens.signal < -0.3 ? "bearish" : "neutral";
        const sigIcon = ens.signal > 0.3 ? "🟢" : ens.signal < -0.3 ? "🔴" : "⚪";
        return (
          <div key={pair} className="report-card" style={{ marginBottom: 16 }}>
            <div className="report-card-header">
              <span className="report-pair">{pair}</span>
              <span className={`signal-badge signal-${sigClass}`}>
                {sigIcon} {ens.signal > 0 ? "+" : ""}{ens.signal.toFixed(3)}
              </span>
              <span style={{ marginLeft: 12, opacity: 0.7 }}>
                {ens.num_indicators} indicators ({ens.multi_dim} cross-asset)
              </span>
            </div>
            <div className="perf-stats" style={{ marginTop: 8 }}>
              <div className="perf-stat"><label>Best Fitness</label><span>{ens.best_fitness.toFixed(2)}</span></div>
              <div className="perf-stat"><label>Best WR</label><span>{(ens.best_wr * 100).toFixed(0)}%</span></div>
            </div>
            {inds.length > 0 && (
              <table className="trade-table" style={{ marginTop: 8, fontSize: "0.85em" }}>
                <thead>
                  <tr><th>Expression</th><th>Fit</th><th>WR</th><th>PnL</th><th>Uses</th></tr>
                </thead>
                <tbody>
                  {inds.map((ind, i) => (
                    <tr key={i}>
                      <td style={{ fontFamily: "monospace", fontSize: "0.8em", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }}
                          title={ind.expr}>{ind.expr}</td>
                      <td>{ind.fitness.toFixed(2)}</td>
                      <td>{(ind.win_rate * 100).toFixed(0)}%</td>
                      <td className={ind.total_pnl > 0 ? "pnl-positive" : ind.total_pnl < 0 ? "pnl-negative" : ""}>
                        {ind.total_pnl > 0 ? "+" : ""}{ind.total_pnl.toFixed(2)}%
                      </td>
                      <td>{ind.uses?.length > 0 ? ind.uses.join(", ") : "price-only"}</td>
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
