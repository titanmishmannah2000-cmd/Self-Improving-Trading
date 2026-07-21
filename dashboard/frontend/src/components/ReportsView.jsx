import React, { useState, useCallback, useEffect } from "react";
import { SkeletonReportCard } from "../Skeleton.jsx";

export default function ReportsView({ apiBase, isActive = true }) {
  const [tab, setTab] = useState("daily");
  const [daily, setDaily] = useState(null);
  const [lifetime, setLifetime] = useState(null);
  const [rangeData, setRangeData] = useState(null);
  const [rangeFrom, setRangeFrom] = useState("");
  const [rangeTo, setRangeTo] = useState("");
  const [rangeFromTime, setRangeFromTime] = useState("00:00");
  const [rangeToTime, setRangeToTime] = useState("23:59");
  const [rangeLoading, setRangeLoading] = useState(false);
  const [rangeError, setRangeError] = useState("");
  const [exportText, setExportText] = useState("");
  const [copied, setCopied] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    if (!isActive) return;
    let cancelled = false;
    async function load() {
      setLoading(true);
      setLoadError(null);
      try {
        const [dRes, lRes] = await Promise.all([
          fetch(`${apiBase}/api/daily-summary`),
          fetch(`${apiBase}/api/lifetime-summary`),
        ]);
        if (!dRes.ok) throw new Error(`daily-summary returned ${dRes.status}`);
        if (!lRes.ok) throw new Error(`lifetime-summary returned ${lRes.status}`);
        const [d, l] = await Promise.all([dRes.json(), lRes.json()]);
        if (!cancelled) { setDaily(d); setLifetime(l); }
      } catch (e) { if (!cancelled) setLoadError(e.message); }
      if (!cancelled) setLoading(false);
    }
    load();
    const id = setInterval(load, 60000);
    const refresh = () => { load(); };
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => { cancelled = true; clearInterval(id); document.removeEventListener("visibilitychange", refresh); window.removeEventListener("focus", refresh); };
  }, [apiBase, isActive]);

  const buildISOTimestamp = (dateStr, timeStr, endOfDay) => {
    if (!dateStr) return null;
    const d = new Date(dateStr + "T" + (timeStr || "00:00") + ":00");
    if (endOfDay) d.setHours(23, 59, 59, 999);
    return d.toISOString();
  };

  const handleRangeSearch = async () => {
    if (!rangeFrom && !rangeTo) { setRangeError("Please pick at least a from or to date"); return; }
    setRangeError(""); setRangeLoading(true); setRangeData(null);
    try {
      const params = new URLSearchParams();
      if (rangeFrom) params.set("from_ts", buildISOTimestamp(rangeFrom, rangeFromTime, false));
      if (rangeTo) params.set("to_ts", buildISOTimestamp(rangeTo, rangeToTime, true));
      const res = await fetch(`${apiBase}/api/range-summary?${params}`);
      setRangeData(await res.json());
    } catch (e) { setRangeError("Failed to fetch range report"); }
    setRangeLoading(false);
  };

  const handleCopy = async () => {
    try {
      const res = await fetch(`${apiBase}/api/export-text`);
      const text = await res.text();
      setExportText(text);
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch (e) { setExportText("Could not reach backend to export."); }
  };

  const formatRangeLabel = (isoStr) => {
    if (!isoStr) return "now";
    const d = new Date(isoStr);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };

  const fmtPct = (n) => {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    const arrow = n > 0 ? "▲ " : n < 0 ? "▼ " : "";
    const sign = n > 0 ? "+" : "";
    return `${arrow}${sign}${Number(n).toFixed(2)}%`;
  };

  const data = tab === "daily" ? daily : tab === "lifetime" ? lifetime : rangeData;

  const summaryLine = (() => {
    if (!data?.bots) return null;
    let closed = 0;
    let pnl = 0;
    for (const b of Object.values(data.bots)) {
      closed += Number(b?.closed_trades) || 0;
      pnl += Number(b?.total_pnl_pct) || 0;
    }
    if (closed === 0) {
      return tab === "daily"
        ? "No finished trades today yet — quiet days are normal."
        : "No finished trades in this window yet.";
    }
    const dir = pnl >= 0 ? "Up" : "Down";
    return `${dir} ${Math.abs(pnl).toFixed(2)}% across ${closed} finished trade${closed === 1 ? "" : "s"}.`;
  })();

  return (
    <div className="reports">
      <div className="reports-head">
        <div className="reports-tabs">
          <button className={`rtab ${tab === "daily" ? "rtab-active" : ""}`} onClick={() => { setTab("daily"); setRangeData(null); }}>Today</button>
          <button className={`rtab ${tab === "lifetime" ? "rtab-active" : ""}`} onClick={() => { setTab("lifetime"); setRangeData(null); }}>All time</button>
          <button className={`rtab ${tab === "custom" ? "rtab-active" : ""}`} onClick={() => setTab("custom")}>Custom</button>
        </div>
        <div className="reports-actions">
          <button className="copy-btn" onClick={handleCopy}>{copied ? "Copied ✓" : "Copy summary"}</button>
        </div>
      </div>

      {summaryLine && (
        <p className="reports-summary-line">{summaryLine}</p>
      )}

      {tab === "custom" && (
        <div className="range-picker">
          <div className="range-fields">
            <div className="range-field"><label>From</label><div className="range-datetime"><input type="date" value={rangeFrom} onChange={(e) => setRangeFrom(e.target.value)} /><input type="time" value={rangeFromTime} onChange={(e) => setRangeFromTime(e.target.value)} /></div></div>
            <div className="range-field"><label>To</label><div className="range-datetime"><input type="date" value={rangeTo} onChange={(e) => setRangeTo(e.target.value)} /><input type="time" value={rangeToTime} onChange={(e) => setRangeToTime(e.target.value)} /></div></div>
            <button className="range-search-btn" onClick={handleRangeSearch} disabled={rangeLoading}>{rangeLoading ? "Loading…" : "Search"}</button>
          </div>
          {rangeError && <p className="range-error">{rangeError}</p>}
          {rangeData && (rangeData.from_ts || rangeData.to_ts) && <p className="range-sublabel">Showing data from {formatRangeLabel(rangeData.from_ts)} to {formatRangeLabel(rangeData.to_ts)}</p>}
        </div>
      )}

      {loading && !loadError && !data && tab !== "custom" && <div className="reports-grid">{[1,2].map(i => <SkeletonReportCard key={i} />)}</div>}
      {loadError && (
        <div className="reports-error">
          <p className="reports-error-msg">Couldn't load results: {loadError}</p>
          <p className="reports-error-hint">Make sure the dashboard server is running. This page retries every 60 seconds.</p>
        </div>
      )}

      {data && (
        <div className="reports-grid">
          {["forex", "gold", "crypto"].map((bot) => {
            const b = data.bots?.[bot];
            if (!b) return null;
            return (
              <div className="report-card" key={bot}>
                <div className="report-card-head">
                  <span className="report-bot-name">{bot === "forex" ? "Currencies" : bot === "gold" ? "Metals" : "Crypto"}</span>
                  {tab === "lifetime" && b.tracking_since && <span className="report-since">since {new Date(b.tracking_since).toLocaleDateString()}</span>}
                </div>
                <div className="report-stats">
                  <div className="rstat"><span className="rstat-num">{b.closed_trades}</span><span className="rstat-label">finished</span></div>
                  <div className="rstat"><span className={`rstat-num ${b.total_pnl_pct >= 0 ? "pc-up" : "pc-down"}`}>{fmtPct(b.total_pnl_pct)}</span><span className="rstat-label">total result</span></div>
                  <div className="rstat"><span className={`rstat-num ${b.win_rate_pct >= 50 ? "pc-up" : "pc-down"}`}>{b.win_rate_pct}%{b.low_confidence ? <span className="vm-lc" title="Low confidence">*</span> : ""}</span><span className="rstat-label">wins</span></div>
                  <div className="rstat"><span className="rstat-num">{b.avg_win_pct > 0 ? "+" : ""}{b.avg_win_pct}/{b.avg_loss_pct}</span><span className="rstat-label">avg win/loss</span></div>
                  {tab === "lifetime" && <div className="rstat"><span className="rstat-num">{b.total_reflections}</span><span className="rstat-label">self-improves</span></div>}
                  {tab === "daily" && <div className="rstat"><span className="rstat-num">{b.reflections_today ?? 0}</span><span className="rstat-label">tweaks today</span></div>}
                  {tab === "custom" && <div className="rstat"><span className="rstat-num">{b.reflections_in_range ?? 0}</span><span className="rstat-label">tweaks</span></div>}
                </div>
                {Object.keys(b.by_pair || {}).length > 0 && (
                  <div className="report-bypair">
                    {Object.entries(b.by_pair).map(([pair, d]) => (
                      <div className="rbp-row" key={pair}><span className="rbp-pair">{pair}</span><span className="rbp-trades">{d.trades} trades</span><span className={`rbp-pnl ${d.total_pnl_pct >= 0 ? "pc-up" : "pc-down"}`}>{fmtPct(d.total_pnl_pct)}</span><span className="rbp-wr">{d.win_rate_pct}% wins</span></div>
                    ))}
                  </div>
                )}
                {(tab === "daily" || tab === "custom") && b.reflections_detail?.length > 0 && (
                  <div className="report-reflections">
                    <div className="dc-label">Settings tweaks {tab === "custom" ? "in range" : "today"}</div>
                    {b.reflections_detail.map((r, i) => <div className="rrefl-row" key={i}><span className="rrefl-pair">{r.pair}</span><span className="rrefl-change">{r.variable}: {r.old} → {r.new}</span></div>)}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {exportText && (
        <div className="export-preview">
          <div className="export-preview-head"><span className="dc-label">Exported text (already copied to clipboard)</span><button className="dfs-close-btn" onClick={() => setExportText("")} title="Close">×</button></div>
          <pre>{exportText}</pre>
        </div>
      )}
    </div>
  );
}
