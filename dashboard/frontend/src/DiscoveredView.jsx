import React, { useState, useEffect, useCallback } from "react";
import { SkeletonCard, SkeletonDiscovered } from "./Skeleton.jsx";

// Cross-asset drivers an indicator may depend on — each gets its own colour dot.
const ASSET_COLORS = {
  volume: "#6cb2ff",
  dxy: "#f4a259",
  vix: "#e15a6d",
  tnx: "#9b8cff",
  spx: "#5fd0a0",
  oil: "#d9b44a",
  gold: "#f2c14e",
  btc: "#f7931a",
  fvx: "#7ec8e3",
  eem: "#c98bdb",
};
const assetColor = (a) => ASSET_COLORS[a] || "#8b93a3";

// Per-indicator quality dot, keyed off out-of-sample win rate.
function statusColor(ind) {
  const wr = Number(ind.win_rate || 0);
  if (wr >= 0.55) return "#3fb950";   // strong
  if (wr >= 0.45) return "#d9b44a";   // marginal
  if (wr > 0) return "#e15a6d";        // weak
  return "#6e7681";                      // no data / seed
}
const statusLabel = (ind) => {
  const wr = Number(ind.win_rate || 0);
  if (wr >= 0.55) return "strong";
  if (wr >= 0.45) return "marginal";
  if (wr > 0) return "weak";
  return ind.source === "discovered" ? "untested" : "seed";
};

function IndicatorRow({ ind }) {
  const wr = Number(ind.win_rate || 0);
  const fit = Number(ind.fitness || 0);
  const pnl = Number(ind.total_pnl || 0);
  const uses = Array.isArray(ind.uses) ? ind.uses : [];
  return (
    <li className="gp-indicator" data-bot={ind._bot}>
      <span className="gp-dot" style={{ background: statusColor(ind) }} title={statusLabel(ind)} />
      <div className="gp-body">
        <div className="gp-name" title={ind.expr}>
          {ind.name || ind.expr || JSON.stringify(ind)}
        </div>
        <div className="gp-stats">
          <span className="gp-stat" title="Out-of-sample win ratio">
            <b className={wr >= 0.55 ? "pc-up" : wr > 0 ? "pc-down" : ""}>
              {wr > 0 ? (wr * 100).toFixed(1) + "%" : "—"}
            </b> win
          </span>
          <span className="gp-stat" title="Fitness score">fit <b>{fit.toFixed(3)}</b></span>
          <span className="gp-stat" title="Total P&L (OOS)">
            PnL <b className={pnl >= 0 ? "pc-up" : "pc-down"}>
              {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)}%
            </b>
          </span>
          {ind.source && (
            <span className={`gp-src gp-src-${ind.source}`}>{ind.source}</span>
          )}
          {ind.discovered_at && ind.discovered_at !== "unknown" && (
            <span className="gp-when">{ind.discovered_at}</span>
          )}
        </div>
        {uses.length > 0 && (
          <div className="gp-assets" title="Cross-asset drivers this indicator uses">
            {uses.map((a) => (
              <span key={a} className="gp-asset">
                <span className="gp-asset-dot" style={{ background: assetColor(a) }} />
                {a}
              </span>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

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
      <p className="discovered-count">
        {data.total_indicators} indicators across {data.total_pairs} pairs
        {data.total_indicators > 0 && (
          <span className="discovered-legend">
            <span className="lg"><i style={{ background: "#3fb950" }} /> strong</span>
            <span className="lg"><i style={{ background: "#d9b44a" }} /> marginal</span>
            <span className="lg"><i style={{ background: "#e15a6d" }} /> weak</span>
            <span className="lg"><i style={{ background: "#6e7681" }} /> seed/untested</span>
          </span>
        )}
      </p>

      {sorted.map((pair) => {
        const inds = Array.isArray(pairs[pair]) ? pairs[pair] : [];
        if (!inds.length) return null;
        const ens = ensemble[pair] || {};
        const signal = ens.signal || 0;
        const sigClass = signal > 0.3 ? "bullish" : signal < -0.3 ? "bearish" : "neutral";
        // tally quality across this pair's indicators
        const strong = inds.filter((i) => Number(i.win_rate || 0) >= 0.55).length;
        const weak = inds.filter((i) => { const w = Number(i.win_rate || 0); return w > 0 && w < 0.45; }).length;
        return (
          <div className="discovered-pair" key={pair}>
            <div className="discovered-pair-head">
              <span className="discovered-pair-name">{pair}</span>
              <span className={`signal-badge signal-${sigClass}`}>
                {signal > 0 ? "+" : ""}{Number(signal).toFixed(3)}
              </span>
              <span className="discovered-pair-meta">
                {inds.length} indicators
                {strong > 0 && <span className="dq dq-strong">{strong}★</span>}
                {weak > 0 && <span className="dq dq-weak">{weak}⚠</span>}
                {ens.multi_dim ? <span className="dq">{ens.multi_dim} cross-asset</span> : null}
              </span>
            </div>
            <ul className="gp-indicators" data-testid="gp-indicators">
              {inds.map((ind, i) => <IndicatorRow key={i} ind={ind} />)}
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
