import React, { useState, useEffect, useCallback, useRef } from "react";
import { API_BASE } from "./config.js";
import { timeAgo } from "./utils.js";
import { COLORS } from "./colors.js";
import { SkeletonAudit } from "./Skeleton.jsx";

// ── helpers ──────────────────────────────────────────────────────────────

const SEVERITY_ICON = {
  CRITICAL: "🔥",
  HIGH: "⚠️",
  MEDIUM: "📌",
  LOW: "💡",
  "N/A": "ℹ️",
};

const SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "N/A"];

function copyText(text) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}

// ── Finding Card ─────────────────────────────────────────────────────────

function FindingCard({ finding, onAction }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const [progress, setProgress] = useState(finding.progress ?? 0);

  const icon = SEVERITY_ICON[finding.severity] || "•";
  const sevClass = finding.severity === "CRITICAL" ? "af-crit" :
    finding.severity === "HIGH" ? "af-high" :
    finding.severity === "MEDIUM" ? "af-med" : "";

  const statusBadge = finding.status === "pending" ? "af-badge-pending" :
    finding.status === "approved" ? "af-badge-approved" :
    finding.status === "in_progress" ? "af-badge-progress" :
    finding.status === "rejected" ? "af-badge-rejected" :
    finding.status === "applied" ? "af-badge-applied" : "";

  // Sync progress when finding data changes
  useEffect(() => { setProgress(finding.progress ?? 0); }, [finding.progress]);

  const handleCopySummary = () => {
    const text = `[${finding.severity}] ${finding.type}: ${finding.description}\n` +
      `Location: ${finding.location || "—"}\n` +
      `Trading Impact: ${finding.trading_impact || "—"}\n` +
      `Suggested Fix: ${finding.suggested_fix || "—"}\n` +
      `Confidence: ${finding.confidence}%`;
    copyText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className={`af-card ${sevClass}`}>
      <div className="af-card-header" onClick={() => setExpanded(!expanded)}>
        <span className="af-icon">{icon}</span>
        <span className={`af-severity ${sevClass}`}>{finding.severity}</span>
        <span className="af-type">{finding.type}</span>
        <span className="af-domain">{finding.domain}</span>
        <span className="af-desc">{finding.description}</span>
        <span className={`af-badge ${statusBadge}`}>{finding.status}</span>
        <span className="af-expand">{expanded ? "▾" : "▸"}</span>
      </div>

      {expanded && (
        <div className="af-card-body">
          <div className="af-row">
            <span className="af-label">Location</span>
            <code className="af-code">{finding.location || "—"}</code>
          </div>
          <div className="af-row">
            <span className="af-label">Trading Impact</span>
            <span>{finding.trading_impact || "—"}</span>
          </div>
          <div className="af-row">
            <span className="af-label">Suggested Fix</span>
            <span className="af-fix">{finding.suggested_fix || "—"}</span>
          </div>
          <div className="af-row">
            <span className="af-label">Confidence</span>
            <span>{finding.confidence}%</span>
          </div>
          <div className="af-row">
            <span className="af-label">Created</span>
            <span>{timeAgo(finding.created_at)}</span>
          </div>

          {/* Progress bar — shown for approved/in_progress */}
          {(finding.status === "approved" || finding.status === "in_progress") && (
            <div className="af-progress-section">
              <div className="af-progress-header">
                <span className="af-label">Progress</span>
                <span className="af-progress-pct">{progress}%</span>
                {finding.assignee && <span className="af-assignee">— {finding.assignee}</span>}
              </div>
              <div className="af-progress-bar-track">
                <div className="af-progress-bar-fill" style={{ width: `${progress}%` }} />
              </div>
              <div className="af-progress-actions">
                {[25, 50, 75, 100].map(pct => (
                  <button
                    key={pct}
                    className={`af-prog-btn ${progress === pct ? "af-prog-active" : ""}`}
                    onClick={() => onAction(finding.id, "progress", pct)}
                  >
                    {pct}%
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="af-actions">
            <button
              className="af-btn af-btn-approve"
              disabled={finding.status !== "pending"}
              onClick={() => onAction(finding.id, "approve")}
            >
              ✓ Approve
            </button>
            <button
              className="af-btn af-btn-reject"
              disabled={finding.status !== "pending"}
              onClick={() => onAction(finding.id, "reject")}
            >
              ✗ Reject
            </button>
            <button
              className="af-btn af-btn-apply"
              disabled={finding.status !== "approved"}
              onClick={() => onAction(finding.id, "apply")}
            >
              ◉ Apply
            </button>
            <button className={`af-btn af-btn-copy ${copied ? "af-btn-copied" : ""}`} onClick={handleCopySummary}>
              {copied ? "Copied ✓" : "Copy Summary"}
            </button>
            {finding.status === "approved" && (
              <button
                className="af-btn af-btn-patch"
                onClick={() => onAction(finding.id, "patch")}
              >
                🔧 Generate Patch
              </button>
            )}
          </div>

          {/* Patch status */}
          {finding.status === "applied" && finding._patch && (
            <div className="af-row">
              <span className="af-label">Patch</span>
              <span className="af-patch-link" onClick={() => onAction(finding.id, "view-patch")}>
                📄 View generated patch
              </span>
            </div>
          )}

          {/* Outcome correlation (PnL impact) */}
          {finding._correlation && (
            <div className="af-correlation">
              <span className="af-label">PnL Impact</span>
              <span className={`af-pnl ${finding._correlation.total_pnl_impact >= 0 ? "pc-up" : "pc-down"}`}>
                {finding._correlation.total_pnl_impact >= 0 ? "+" : ""}{finding._correlation.total_pnl_impact}%
              </span>
              <span className="af-correlation-detail">
                {finding._correlation.potential_trades_found} trades · {finding._correlation.win_rate_impact}% WR
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Maturity Chart ───────────────────────────────────────────────────────

function MaturityChart({ history, domain }) {
  if (!history || history.length < 2) {
    return <p className="detail-muted">Need at least 2 data points for a trend.</p>;
  }

  const w = 300, h = 60, pad = { top: 4, bottom: 16, left: 20, right: 8 };
  const scores = history.map(h => h.score);
  const min = 0, max = 5;
  const rng = max - min || 1;
  const xStep = (w - pad.left - pad.right) / (scores.length - 1 || 1);

  const points = scores.map((s, i) => ({
    x: pad.left + i * xStep,
    y: h - pad.bottom - ((s - min) / rng) * (h - pad.top - pad.bottom),
    score: s,
  }));

  const line = points.map(p => `${p.x},${p.y}`).join(" ");
  const color = scores[scores.length - 1] >= scores[0] ? COLORS.up : COLORS.down;

  return (
    <div className="af-maturity-chart">
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
        {/* Grid lines */}
        {[1,2,3,4,5].map(v => {
          const y = h - pad.bottom - ((v - min) / rng) * (h - pad.top - pad.bottom);
          return (
            <g key={v}>
              <line x1={pad.left} y1={y} x2={w - pad.right} y2={y} stroke={COLORS.chartGrid} strokeWidth={0.5} />
              <text x={pad.left - 4} y={y + 3} textAnchor="end" fill={COLORS.chartLabel} fontSize={9}>{v}</text>
            </g>
          );
        })}
        <polyline fill="none" stroke={color} strokeWidth={1.5} points={line} />
        {points.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={2.5} fill={color} />
        ))}
      </svg>
      <div className="af-maturity-label">{domain}</div>
    </div>
  );
}

// ── Main AuditView ───────────────────────────────────────────────────────

export default function AuditView({ apiBase, isActive = true }) {
  const [findings, setFindings] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("all");
  const [recentlyApplied, setRecentlyApplied] = useState([]);
  const [showRecentlyFixed, setShowRecentlyFixed] = useState(true);
  const [showArchive, setShowArchive] = useState(false);
  const [auditRunning, setAuditRunning] = useState(false);
  const [auditProgress, setAuditProgress] = useState(null);
  const [auditRunId, setAuditRunId] = useState(null);

  const loadData = useCallback(async () => {
    try {
      const [fRes, sRes, aRes] = await Promise.all([
        fetch(`${apiBase}/api/audit/findings?limit=100`),
        fetch(`${apiBase}/api/audit/summary`),
        fetch(`${apiBase}/api/audit/findings?status=applied&limit=20`),
      ]);
      if (fRes.ok) {
        const fData = await fRes.json();
        setFindings(fData.findings || []);
      }
      if (sRes.ok) {
        const sData = await sRes.json();
        setSummary(sData.stats || null);
      }
      if (aRes.ok) {
        const aData = await aRes.json();
        setRecentlyApplied(aData.findings || []);
      }
      setError(null);
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, [apiBase]);

  useEffect(() => {
    if (!isActive) return;
    loadData();
    const id = setInterval(loadData, 30000);
    const refresh = () => { loadData(); };
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", refresh); window.removeEventListener("focus", refresh); };
  }, [loadData, isActive]);

  const handleAction = async (findingId, action, extra) => {
    try {
      if (action === "patch") {
        const res = await fetch(`${apiBase}/api/audit/findings/${findingId}/patch`, { method: "POST" });
        if (res.ok) {
          const data = await res.json();
          if (data.status === "ok" && data.patchable) {
            alert(`✅ Patch generated!\nFile: ${data.patch_file}\nRisk: ${data.risk}\n\n${data.explanation}`);
          } else {
            alert(`⚠️ Patch generation: ${data.message || data.explanation || "Could not generate patch"}`);
          }
        }
        loadData();
      } else if (action === "view-patch") {
        window.open(`${apiBase}/api/audit/findings/${findingId}/patch`, "_blank");
      } else if (action === "progress") {
        const pct = extra;
        await fetch(`${apiBase}/api/audit/findings/${findingId}/progress`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ progress: pct, assignee: "Trevor" }),
        });
        loadData();
      } else {
        const res = await fetch(`${apiBase}/api/audit/findings/${findingId}/${action}`, { method: "POST" });
        if (res.ok) loadData();
      }
    } catch (e) {
      console.error("Audit action failed:", e);
    }
  };

  // ── NLI Ask ──
  const [askQuestion, setAskQuestion] = useState("");
  const [askAnswer, setAskAnswer] = useState(null);
  const [askLoading, setAskLoading] = useState(false);

  const handleAsk = async (quick = false) => {
    if (!askQuestion.trim()) return;
    setAskLoading(true);
    setAskAnswer(null);
    try {
      const res = await fetch(`${apiBase}/api/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: askQuestion, quick }),
      });
      if (res.ok) {
        const data = await res.json();
        setAskAnswer(data);
      } else {
        setAskAnswer({ answer: `Error: ${res.status}`, mode: "error" });
      }
    } catch (e) {
      setAskAnswer({ answer: `Error: ${e.message}`, mode: "error" });
    }
    setAskLoading(false);
  };

  // ── Correlate findings with PnL ──
  const correlateFindings = async () => {
    try {
      const res = await fetch(`${apiBase}/api/audit/correlate`);
      if (res.ok) {
        const data = await res.json();
        if (data.correlations) {
          // Merge correlations into findings
          setFindings(prev => prev.map(f => ({
            ...f,
            _correlation: data.correlations[f.id] || null,
          })));
        }
      }
    } catch (e) {
      console.error("Correlation failed:", e);
    }
  };

  // Load correlations on fresh data only, not on filter changes — and only when visible
  const hasCorrelated = useRef(false);
  useEffect(() => {
    if (!isActive) return;
    if (findings.length > 0 && !hasCorrelated.current) {
      hasCorrelated.current = true;
      correlateFindings();
    }
  }, [findings.length, isActive]);
  // Reset correlation flag when new audit data is loaded
  useEffect(() => {
    if (!loading) hasCorrelated.current = false;
  }, [loading]);

  const triggerRun = async () => {
    if (auditRunning) return;
    try {
      setAuditRunning(true);
      setAuditProgress({ progress_pct: 0, message: "Starting..." });
      const res = await fetch(`${apiBase}/api/audit/run`, { method: "POST" });
      const data = await res.json();
      if (data.run_id) {
        setAuditRunId(data.run_id);
        // Poll progress
        let pollId = setInterval(async () => {
          try {
            const pr = await fetch(`${apiBase}/api/audit/progress/${data.run_id}`);
            const pd = await pr.json();
            if (pd.progress) {
              setAuditProgress(pd.progress);
              if (pd.progress.status === "complete" || pd.progress.status === "error") {
                clearInterval(pollId);
                setAuditRunning(false);
                setTimeout(loadData, 1000);
              }
            }
          } catch (e) {
            clearInterval(pollId);
            setAuditRunning(false);
          }
        }, 2000);
      }
    } catch (e) {
      setAuditRunning(false);
      console.error("Trigger audit failed:", e);
    }
  };

  // ── Bulk action ──
  const handleBulkAction = async (action, severity) => {
    try {
      const res = await fetch(`${apiBase}/api/audit/bulk-action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, severity }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.count > 0) loadData();
      }
    } catch (e) {
      console.error("Bulk action failed:", e);
    }
  };
  const filtered = filter === "all" ? (showArchive ? findings.filter(f => f.status !== "applied") : findings) :
    filter === "open" ? findings.filter(f => f.status === "pending" || f.status === "approved") :
    findings.filter(f => f.status === filter);

  // Group by severity for display
  const grouped = {};
  for (const f of filtered) {
    const key = SEVERITY_ORDER.includes(f.severity) ? f.severity : "N/A";
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(f);
  }

  if (loading) return <SkeletonAudit />;
  if (error) return <div className="detail-muted">Error: {error}</div>;

  return (
    <div className="audit-view">
      {/* ── Header ── */}
      <div className="audit-header">
        <div>
          <h2>Self-Audit</h2>
          <p className="app-sub">Autonomous code, risk, and performance audit — findings require your approval before any changes.</p>
        </div>
        <button className={`audit-run-btn ${auditRunning ? "audit-running" : ""}`} onClick={triggerRun} disabled={auditRunning} title="Trigger a new audit run">
          {auditRunning ? "⏳ Auditing..." : "▶ Run Audit"}
        </button>
      </div>

      {/* ── Audit Progress Bar ── */}
      {auditRunning && auditProgress && (
        <div className="audit-progress-bar">
          <div className="ap-fill" style={{ width: `${auditProgress.progress_pct || 0}%` }}></div>
          <span className="ap-text">
            {auditProgress.message || `Auditing... ${auditProgress.progress_pct || 0}%`}
            {auditProgress.current_domain && ` — ${auditProgress.current_domain}`}
          </span>
        </div>
      )}
      {!auditRunning && auditProgress && auditProgress.status === "complete" && (
        <div className="audit-progress-complete">✅ Audit complete — {auditProgress.message}</div>
      )}
      {!auditRunning && auditProgress && auditProgress.status === "error" && (
        <div className="audit-progress-error">❌ Audit failed — {auditProgress.message}</div>
      )}

      {/* ── Summary bar ── */}
      {summary && (
        <div className="audit-summary-bar">
          <div className="as-item">
            <span className="as-num as-num-total">{summary.total_findings}</span>
            <span className="as-label">Total</span>
          </div>
          <div className="as-item">
            <span className="as-num as-num-open">{summary.open_findings}</span>
            <span className="as-label">Open</span>
          </div>
          <div className="as-item">
            <span className="as-num as-num-critical">{summary.critical_open}</span>
            <span className="as-label">Critical Open</span>
          </div>
          <div className="as-item">
            <span className="as-num as-num-applied">{summary.applied_findings}</span>
            <span className="as-label">Applied</span>
          </div>
          <div className="as-item">
            <span className="as-num as-num-rejected">{summary.rejected_findings}</span>
            <span className="as-label">Rejected</span>
          </div>
          {summary.latest_run_at && (
            <div className="as-item as-last-run">
              <span className="as-label">Last run</span>
              <span>{timeAgo(summary.latest_run_at)}</span>
            </div>
          )}
        </div>
      )}

      {/* ── Recently Fixed ── */}
      {showRecentlyFixed && recentlyApplied.length > 0 && (
        <div className="audit-recently-fixed">
          <div className="arf-header">
            <span className="arf-title">✅ Recently Fixed</span>
            <span className="arf-count">{recentlyApplied.length} fix{recentlyApplied.length !== 1 ? "es" : ""} applied</span>
            <button className="arf-dismiss" onClick={() => setShowRecentlyFixed(false)} title="Dismiss">×</button>
          </div>
          <div className="arf-list">
            {recentlyApplied.slice(0, 8).map(f => (
              <div className="arf-item" key={f.id}>
                <span className={`arf-sev ${f.severity === "CRITICAL" ? "arf-crit" : f.severity === "HIGH" ? "arf-high" : ""}`}>
                  {SEVERITY_ICON[f.severity] || "✅"}
                </span>
                <span className="arf-desc">{f.description}</span>
                <span className="arf-domain">{f.domain}</span>
                <span className="arf-time">{timeAgo(f.updated_at || f.created_at)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Maturity cards ── */}
      {summary?.latest_maturity && Object.keys(summary.latest_maturity).length > 0 && (
        <div className="audit-maturity-row">
          {Object.entries(summary.latest_maturity).map(([domain, scores]) => (
            <div className="audit-maturity-card" key={domain}>
              <div className="am-domain">{domain}</div>
              <div className="am-score-row">
                <span className={`am-score ${scores.score >= 3 ? "am-score-ok" : "am-score-warn"}`}>
                  {scores.score}/5
                </span>
                <span className="am-compare">{scores.compared_to_prior}</span>
              </div>
              <span className="am-intel">Intel: {scores.intelligence_score}/5</span>
              {summary.maturity_history?.[domain]?.length >= 2 && (
                <MaturityChart history={summary.maturity_history[domain]} domain={domain} />
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── Ask NLI ── */}
      <div className="audit-ask-bar">
        <input
          className="audit-ask-input"
          type="text"
          placeholder='Ask anything — "What&apos;s wrong right now?"'
          value={askQuestion}
          onChange={(e) => setAskQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleAsk(true); }}
        />
        <button className="audit-ask-btn" onClick={() => handleAsk(true)} disabled={askLoading}>
          {askLoading ? "…" : "Ask"}
        </button>
        <button className="audit-ask-btn audit-ask-btn-deep" onClick={() => handleAsk(false)} disabled={askLoading} title="Deep (LLM-powered)">
          ✦ Deep
        </button>
      </div>

      {/* Ask result */}
      {askAnswer && (
        <div className={`audit-ask-result ${askAnswer.has_critical ? "audit-ask-critical" : ""}`}>
          <div className="ask-answer">{askAnswer.answer}</div>
          {askAnswer.critical_items?.length > 0 && (
            <div className="ask-critical-items">
              {askAnswer.critical_items.map((item, i) => (
                <div key={i} className="ask-critical-item">🔴 {item}</div>
              ))}
            </div>
          )}
          <div className="ask-meta">
            {askAnswer.mode && <span className="ask-mode">{askAnswer.mode}</span>}
            {askAnswer.confidence && <span className="ask-confidence">confidence: {askAnswer.confidence}</span>}
            <button className="ask-dismiss" onClick={() => setAskAnswer(null)}>×</button>
          </div>
        </div>
      )}

      {/* ── Filter tabs ── */}
      <div className="audit-filter-row">
        {["all", "open", "pending", "approved", "rejected", "applied"].map(f => (
          <button
            key={f}
            className={`af-filter-btn ${filter === f ? "af-filter-active" : ""}`}
            onClick={() => setFilter(f)}
          >
            {f === "all" ? "All" : f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
        <button className="af-filter-btn af-filter-archive" onClick={() => setShowArchive(!showArchive)} title="Hide applied findings from All view">
          {showArchive ? "⊟ Archived" : "⊞ Archive"}
        </button>
      </div>

      {/* ── Findings list ── */}
      <div className="audit-findings-list">
        {filtered.length === 0 && (
          <p className="detail-muted">No findings match this filter.</p>
        )}
        {SEVERITY_ORDER.map(sev => {
          const items = grouped[sev] || [];
          if (items.length === 0) return null;
          return (
            <div key={sev}>
              <div className="af-severity-group">
                {SEVERITY_ICON[sev]} {sev} ({items.length})
                {(sev === "CRITICAL" || sev === "HIGH" || sev === "MEDIUM") && items.some(f => f.status === "pending") && (
                  <span className="af-bulk-actions">
                    <button className="af-bulk-btn af-bulk-approve" onClick={() => handleBulkAction("approve", sev)} title={`Approve all ${sev} findings`}>
                      ✓ All
                    </button>
                    <button className="af-bulk-btn af-bulk-reject" onClick={() => handleBulkAction("reject", sev)} title={`Reject all ${sev} findings`}>
                      ✗ All
                    </button>
                  </span>
                )}
              </div>
              {items.map(f => (
                <FindingCard key={f.id} finding={f} onAction={handleAction} />
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
