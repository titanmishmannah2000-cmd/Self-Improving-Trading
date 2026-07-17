import React, { useState, useEffect, useCallback } from "react";
import { SkeletonCard, SkeletonCortex } from "./Skeleton.jsx";

export default function CortexView({ apiBase, isActive = true }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeBot, setActiveBot] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const r = await fetch(`${apiBase}/api/cortex`);
      if (!r.ok) throw new Error(`API ${r.status}`);
      setData(await r.json());
      setError(null);
    } catch (e) { setError(e.message); }
    setLoading(false);
  }, [apiBase]);

  useEffect(() => { if (!isActive) return; fetchData(); const iv = setInterval(fetchData, 30000); const refresh = () => { fetchData(); }; document.addEventListener("visibilitychange", refresh); window.addEventListener("focus", refresh); return () => { clearInterval(iv); document.removeEventListener("visibilitychange", refresh); window.removeEventListener("focus", refresh); }; }, [fetchData, isActive]);

  if (loading) return <SkeletonCortex />;
  if (error) return <div className="reports"><p className="error">{error} — <button className="retry-inline" onClick={fetchData}>retry</button></p></div>;

  const botEntries = Object.entries(data || {}).filter(([k]) => k !== "status");
  const botNames = botEntries.map(([k]) => k);
  // Auto-select first bot, or keep current selection if still present
  const currentTab = activeBot && botNames.includes(activeBot) ? activeBot : (botNames[0] || null);
  const botData = currentTab ? (data || {})[currentTab] : null;

  if (botEntries.length === 0)
    return <div className="reports"><p>No cortex data yet. Bot will push data on its heartbeat cycle.</p></div>;

  const s = (botData && botData.summary) || {};
  const exiled = (botData && botData.exiled) || [];
  const indicators = (botData && botData.indicators) || {};
  const policy = (botData && botData.policy) || {};
  const byType = s.by_entry_type || {};
  const byPair = s.by_pair || {};
  const suppressions = policy.suppressions || {};
  const allocation = policy.allocation || {};
  const prioDisc = policy.priority_discovery || [];
  const rollback = policy.rollback_candidates || [];

  return (
    <div className="reports">
      <div className="reports-head">
        <div className="reports-title">Decision Cortex</div>
        <div className="reports-subtitle">Entry-type performance, exiles, indicators, and policy</div>
      </div>

      {/* Bot tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 16, borderBottom: "1px solid #333" }}>
        {botEntries.map(([botName]) => (
          <button
            key={botName}
            onClick={() => setActiveBot(botName)}
            style={{
              padding: "8px 16px",
              cursor: "pointer",
              border: "none",
              borderBottom: currentTab === botName ? "2px solid #4a9eff" : "2px solid transparent",
              background: "transparent",
              color: currentTab === botName ? "#4a9eff" : "#999",
              fontWeight: currentTab === botName ? 600 : 400,
              fontSize: "0.9em",
              textTransform: "uppercase",
            }}
          >
            {botName}
          </button>
        ))}
      </div>

      <div key={currentTab}>
        {/* Summary header */}
        <div className="report-card" style={{ marginBottom: 16 }}>
          <div className="report-card-header">
            <span className="report-pair">{currentTab.toUpperCase()}</span>
            <span style={{ marginLeft: 12, opacity: 0.7 }}>
              {s.entries_total || 0} completed{s.entries_open ? ` (${s.entries_open} open)` : ""} · {s.exiled_indicators || 0} exiled · {s.indicators_tracked || 0} tracked · v{policy.version || "?"}
            </span>
          </div>
        </div>

        {/* Policy: Active Suppressions + Allocation */}
        {Object.keys(suppressions).length > 0 || Object.keys(allocation).length > 0 ? (
          <div className="report-card" style={{ marginBottom: 16, borderLeft: "3px solid #8855ff" }}>
            <div className="report-card-header" style={{ color: "#8855ff" }}>⚙️ Active Policy Decisions</div>
            {Object.keys(suppressions).length > 0 && (
              <table className="trade-table" style={{ marginTop: 8, fontSize: "0.85em" }}>
                <thead><tr><th>Pair</th><th>Suppressed</th><th>Allocation (MR / RSI / GP)</th></tr></thead>
                <tbody>
                  {Object.entries(suppressions).map(([pair, sup]) => {
                    const alloc = allocation[pair] || {};
                    return (
                      <tr key={pair}>
                        <td>{pair}</td>
                        <td>{sup.gp_ensemble ? "🚫 GP suppressed" : "—"}</td>
                        <td style={{ fontSize: "0.85em" }}>
                          MR: {alloc.mean_reversion || 0}% / RSI: {alloc.rsi_momentum || 0}% / GP: {alloc.gp_ensemble || 0}%
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
            {prioDisc.length > 0 && <p style={{ margin: "4px 0", fontSize: "0.85em" }}>🔬 Priority discovery needed: {prioDisc.join(", ")}</p>}
            {rollback.length > 0 && <p style={{ margin: "4px 0", fontSize: "0.85em", color: "#ffaa00" }}>⚠️ Rollback candidates: {rollback.map(r => `${r.pair} v${r.version}`).join(", ")}</p>}
          </div>
        ) : null}

        {/* Entry-type breakdown */}
        {Object.keys(byType).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16 }}>
            <div className="report-card-header">📊 Performance by Entry Type</div>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.85em" }}>
              <thead><tr><th>Type</th><th>Trades</th><th>Wins</th><th>WR</th><th>PnL</th></tr></thead>
              <tbody>
                {Object.entries(byType).map(([t, st]) => (
                  <tr key={t}>
                    <td>{t === "gp_ensemble" ? "GP Ensemble" : t === "mean_reversion" ? "Mean Reversion" : t === "rsi_momentum" ? "RSI Momentum" : t}</td>
                    <td>{st.n}</td><td>{st.wins}</td>
                    <td className={st.n > 0 && st.wins / st.n > 0.5 ? "pnl-positive" : "pnl-negative"}>
                      {st.n > 0 ? (st.wins / st.n * 100).toFixed(0) : 0}%
                    </td>
                    <td className={st.pnl > 0 ? "pnl-positive" : st.pnl < 0 ? "pnl-negative" : ""}>
                      {st.pnl > 0 ? "+" : ""}{st.pnl.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Per-pair breakdown */}
        {Object.keys(byPair).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16 }}>
            <div className="report-card-header">📋 Per-Pair Totals</div>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.85em" }}>
              <thead><tr><th>Pair</th><th>Trades</th><th>Wins</th><th>WR</th><th>PnL</th></tr></thead>
              <tbody>
                {Object.entries(byPair).map(([p, st]) => (
                  <tr key={p}>
                    <td>{p}</td><td>{st.n}</td><td>{st.wins}</td>
                    <td className={st.n > 0 && st.wins / st.n > 0.5 ? "pnl-positive" : "pnl-negative"}>
                      {st.n > 0 ? (st.wins / st.n * 100).toFixed(0) : 0}%
                    </td>
                    <td className={st.pnl > 0 ? "pnl-positive" : st.pnl < 0 ? "pnl-negative" : ""}>
                      {st.pnl > 0 ? "+" : ""}{st.pnl.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Per-indicator WR table */}
        {Object.keys(indicators).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16, borderLeft: "3px solid #00aaff" }}>
            <div className="report-card-header" style={{ color: "#00aaff" }}>🔍 Per-Indicator Live WR</div>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.82em" }}>
              <thead><tr><th>Indicator</th><th>Entries</th><th>Wins</th><th>WR</th><th>PnL</th><th>GP-Entry WR</th></tr></thead>
              <tbody>
                {Object.entries(indicators).map(([name, ind]) => {
                  const gp = ind.by_type?.gp_ensemble || {};
                  const gpWr = gp.entries > 0 ? (gp.wins / gp.entries * 100).toFixed(0) : "—";
                  const totalWr = ind.entries > 0 ? (ind.wins / ind.entries * 100).toFixed(0) : 0;
                  return (
                    <tr key={name}>
                      <td style={{ fontFamily: "monospace", maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}>{name}</td>
                      <td>{ind.entries}</td><td>{ind.wins}</td>
                      <td className={parseFloat(totalWr) > 50 ? "pnl-positive" : "pnl-negative"}>{totalWr}%</td>
                      <td className={ind.pnl > 0 ? "pnl-positive" : "pnl-negative"}>{ind.pnl > 0 ? "+" : ""}{ind.pnl.toFixed(2)}%</td>
                      <td className={gpWr !== "—" && parseFloat(gpWr) > 30 ? "pnl-positive" : "pnl-negative"}>{gpWr}%</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Exiled */}
        {exiled.length > 0 && (
          <div className="report-card" style={{ marginBottom: 16, borderLeft: "3px solid #ff4444" }}>
            <div className="report-card-header"><span style={{ color: "#ff4444" }}>🚫 Exiled Indicators</span> <span style={{ opacity: 0.7 }}>({exiled.length})</span></div>
            <table className="trade-table" style={{ marginTop: 8, fontSize: "0.85em" }}>
              <thead><tr><th>Indicator</th></tr></thead>
              <tbody>{exiled.map((n, i) => <tr key={i}><td style={{ fontFamily: "monospace" }}>{n}</td></tr>)}</tbody>
            </table>
          </div>
        )}

        {/* Policy decisions log */}
        {policy.decisions && policy.decisions.length > 0 && (
          <div className="report-card" style={{ marginBottom: 16, borderLeft: "3px solid #8855ff" }}>
            <div className="report-card-header" style={{ color: "#8855ff" }}>📝 Policy Decision Log</div>
            <div style={{ fontSize: "0.8em", maxHeight: 300, overflowY: "auto" }}>
              {policy.decisions.slice().reverse().map((d, i) => (
                <div key={i} style={{ padding: "2px 0", borderBottom: "1px solid #222" }}>
                  <span style={{ opacity: 0.5 }}>{d.ts.slice(11, 19)}</span> {d.text}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
