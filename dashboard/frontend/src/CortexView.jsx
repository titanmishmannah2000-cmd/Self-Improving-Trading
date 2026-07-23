import React, { useState, useEffect, useCallback } from "react";
import { SkeletonCortex } from "./Skeleton.jsx";

/** Plain-language blurb shown under each section header. */
function Help({ children }) {
  return <p className="cortex-help">{children}</p>;
}

function fmtType(t) {
  if (t === "gp_ensemble") return "GP Ensemble (GP Brain)";
  if (t === "mean_reversion") return "Mean Reversion";
  if (t === "rsi_momentum") return "RSI Momentum";
  return t || "—";
}

function isSuppressed(sup, type) {
  if (Array.isArray(sup)) return sup.includes(type);
  if (sup && typeof sup === "object") return Boolean(sup[type]);
  return false;
}

function suppressedLabel(sup) {
  const types = Array.isArray(sup)
    ? sup
    : Object.keys(sup || {}).filter((k) => sup[k]);
  if (!types.length) return "None — both styles allowed";
  return types.map((t) => `${fmtType(t)} benched`).join(", ");
}

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
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, [apiBase]);

  useEffect(() => {
    if (!isActive) return;
    fetchData();
    const iv = setInterval(fetchData, 30000);
    const refresh = () => {
      fetchData();
    };
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      clearInterval(iv);
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [fetchData, isActive]);

  if (loading) return <SkeletonCortex />;
  if (error) {
    return (
      <div className="reports">
        <p className="error">
          {error} —{" "}
          <button className="retry-inline" onClick={fetchData}>
            retry
          </button>
        </p>
      </div>
    );
  }

  const botEntries = Object.entries(data || {}).filter(
    ([k]) => k !== "status" && k !== "error" && typeof (data || {})[k] === "object"
  );
  const botNames = botEntries.map(([k]) => k);
  const currentTab =
    activeBot && botNames.includes(activeBot) ? activeBot : botNames[0] || null;
  const botData = currentTab ? (data || {})[currentTab] : null;

  if (botEntries.length === 0) {
    return (
      <div className="reports">
        <div className="reports-head">
          <div className="reports-title">Decision Cortex</div>
          <div className="reports-subtitle">
            The bot&apos;s memory of what worked — and what it has stopped using
          </div>
        </div>
        <div className="report-card cortex-empty">
          <Help>
            No cortex data yet. After the bot closes a few trades it will remember
            which entry styles (Mean Reversion vs GP Brain) and which discovered
            indicators are winning or losing. That memory is what this tab shows.
          </Help>
          <p className="detail-muted">
            Waiting for the next heartbeat push from forex / gold / crypto.
          </p>
        </div>
      </div>
    );
  }

  const s = (botData && botData.summary) || {};
  const exiled = (botData && botData.exiled) || [];
  const indicators = (botData && botData.indicators) || {};
  const policy = (botData && botData.policy) || {};
  const gates = (botData && botData.gates) || policy.gates || {};
  const byType = (botData && botData.by_entry_type) || {};
  const byPair = (botData && botData.by_pair) || {};
  const typeWr = (botData && botData.type_wr) || {};
  const suppressions = policy.suppressions || {};
  const allocation = policy.allocation || {};
  const prioDisc = policy.priority_discovery;
  const rollback = policy.rollback;
  const probeEv = (botData && botData.probe_evidence) || {};
  const probeByKey = probeEv.by_key || {};
  const probeThreshold = probeEv.threshold ?? 5;

  const hasPolicyBlock =
    Object.keys(suppressions).length > 0 ||
    Object.keys(allocation).length > 0 ||
    policy.soft_weights === true ||
    prioDisc === true ||
    (Array.isArray(prioDisc) && prioDisc.length > 0) ||
    rollback === true ||
    (Array.isArray(rollback) && rollback.length > 0) ||
    Object.keys(gates).length > 0;

  return (
    <div className="reports cortex-view">
      <div className="reports-head">
        <div className="reports-title">Decision Cortex</div>
        <div className="reports-subtitle">
          Memory + governance: which entry styles and GP indicators are earning
          their keep
        </div>
      </div>

      <div className="report-card cortex-intro">
        <Help>
          Think of the Cortex as the bot&apos;s scoreboard. Every closed trade
          updates win-rate by entry type and by discovered indicator. Poor
          performers get <strong>exiled</strong> (temporarily removed from the GP
          vote). The <strong>policy</strong> layer can bench a whole entry style
          (e.g. pause GP Brain when Mean Reversion is clearly better).
        </Help>
      </div>

      <div className="cortex-bot-tabs" role="tablist" aria-label="Bot cortex">
        {botEntries.map(([botName]) => (
          <button
            key={botName}
            role="tab"
            aria-selected={currentTab === botName}
            className={`cortex-bot-tab ${currentTab === botName ? "active" : ""}`}
            onClick={() => setActiveBot(botName)}
          >
            {botName}
          </button>
        ))}
      </div>

      <div key={currentTab}>
        <div className="report-card" style={{ marginBottom: 16 }}>
          <div className="report-card-header">
            <span className="report-pair">{currentTab.toUpperCase()} snapshot</span>
          </div>
          <Help>
            Totals for this bot since cortex memory started. &quot;Best style&quot;
            is the router&apos;s current favorite based on closed-trade win-rate.
          </Help>
          <div className="cortex-stat-row">
            <div className="cortex-stat">
              <span className="cortex-stat-num">{s.entries_total || 0}</span>
              <span className="cortex-stat-label">closed trades remembered</span>
            </div>
            <div className="cortex-stat">
              <span className="cortex-stat-num">{s.entries_open || 0}</span>
              <span className="cortex-stat-label">open (not yet scored)</span>
            </div>
            <div className="cortex-stat">
              <span className="cortex-stat-num">{s.indicators_tracked || 0}</span>
              <span className="cortex-stat-label">indicators tracked</span>
            </div>
            <div className="cortex-stat">
              <span className="cortex-stat-num">{s.exiled_indicators || 0}</span>
              <span className="cortex-stat-label">currently exiled</span>
            </div>
            <div className="cortex-stat">
              <span className="cortex-stat-num cortex-stat-best">
                {fmtType(s.best_entry_type)}
              </span>
              <span className="cortex-stat-label">best entry style right now</span>
            </div>
          </div>
        </div>

        <div className="report-card" style={{ marginBottom: 16 }} data-testid="cortex-probe-evidence">
          <div className="report-card-header">Probe sizing evidence</div>
          <Help>
            HIF Phase 1: when <code>PROBE_SIZING=1</code>, pairs/styles with fewer than{" "}
            {probeThreshold} closed cortex outcomes open at 25% size (probe). At{" "}
            {probeThreshold}+ closed outcomes they use full size. This never blocks a
            trade — it only shrinks risk while learning. Flag default is OFF.
          </Help>
          {Object.keys(probeByKey).length === 0 ? (
            <p className="detail-muted">
              No closed outcomes remembered yet — if Probe Sizing is enabled, new
              entries will open in probe mode until evidence builds.
            </p>
          ) : (
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.85em" }}>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Entry style</th>
                  <th>Closed</th>
                  <th>Evidence</th>
                  <th>If enabled</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(probeByKey).map(([key, row]) => (
                  <tr key={key}>
                    <td>{row.pair || key.split("|")[0]}</td>
                    <td>{fmtType(row.entry_type)}</td>
                    <td>{row.evidence_n ?? row.n ?? 0}</td>
                    <td>
                      <span className={row.evidence_state === "thin" ? "dp-probe" : "dp-full"}>
                        {row.evidence_state || "—"}
                      </span>
                    </td>
                    <td>
                      <span className={row.size_mode_if_enabled === "probe" ? "dp-probe" : "dp-full"}>
                        {(row.size_mode_if_enabled || "full").toUpperCase()}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="report-card" style={{ marginBottom: 16 }} data-testid="cortex-regime-sizing">
          <div className="report-card-header">Regime sizing (Phase 3)</div>
          <Help>
            When <code>REGIME_SIZING=1</code>, open size is scaled by market mood from
            indicators: uptrend ≈ 100%, range ≈ 85%, downtrend ≈ 40% (soft ADX blend).
            Watch Live badges like <strong>R40%</strong> on open trades. Never blocks
            entries — rollback with <code>REGIME_SIZING=0</code>.
          </Help>
          <p className="detail-muted">
            Per-open details (mult + label) appear on Live pair cards and the detail
            panel after the next bot ingest.
          </p>
        </div>

        <div className="report-card" style={{ marginBottom: 16 }} data-testid="cortex-skip-shadow">
          <div className="report-card-header">Skip / shadow learning (Phase 4)</div>
          <Help>
            When <code>SKIP_SHADOW_REFLECT=1</code>, quiet pairs still write{" "}
            <strong>shadow hypotheses</strong> from skip reasons and GP shadow votes
            (Activity feed). These never auto-change strategy YAML — they keep
            intelligence moving while trade count is low. Rollback:{" "}
            <code>SKIP_SHADOW_REFLECT=0</code>.
          </Help>
          <p className="detail-muted">
            Look for hypothesis status <code>skip_shadow_note</code> /{" "}
            <code>skip_shadow_proposed</code> in Activity / Reports.
          </p>
        </div>

        <div className="report-card" style={{ marginBottom: 16 }} data-testid="cortex-kelly-sizing">
          <div className="report-card-header">Kelly sizing (Phase 5)</div>
          <Help>
            When <code>KELLY_SIZING=1</code>, open size is scaled by a cautious
            quarter-Kelly fraction from cortex win-rate (Bayesian) and win/loss or
            TP/SL odds. Live badge <strong>Kxx%</strong> and detail show p̂ + CI.
            No history → no change. Never blocks. Rollback: <code>KELLY_SIZING=0</code>.
          </Help>
        </div>

        <div className="report-card" style={{ marginBottom: 16 }} data-testid="cortex-entry-ranking">
          <div className="report-card-header">Entry ranking (Layer B)</div>
          <Help>
            When <code>ENTRY_RANKING=1</code>, if traditional and GP both could
            fire, Hermes scores expected edge and opens the better candidate.
            Live badge <strong>Rank x.xx</strong> and detail show why. Never
            hard-blocks — one candidate still opens. Rollback:{" "}
            <code>ENTRY_RANKING=0</code>.
          </Help>
        </div>

        {hasPolicyBlock && (
          <div className="report-card cortex-policy" style={{ marginBottom: 16 }}>
            <div className="report-card-header">Active policy decisions</div>
            <Help>
              Policy looks at cortex win-rates and can temporarily stop one entry
              style from opening new trades. This does not close positions already
              open — it only blocks new entries of the benched type.
            </Help>
            {(gates.suppress_gp || gates.exile || gates.probe) && (
              <ul className="cortex-gates">
                {gates.suppress_gp && <li>{gates.suppress_gp}</li>}
                {gates.suppress_mr && <li>{gates.suppress_mr}</li>}
                {gates.priority_discovery && <li>{gates.priority_discovery}</li>}
                {gates.rollback && <li>{gates.rollback}</li>}
                {gates.exile && <li>{gates.exile}</li>}
                {gates.reinstate && <li>{gates.reinstate}</li>}
                {gates.probe && <li>{gates.probe}</li>}
                {gates.soft_weights && <li>{gates.soft_weights}</li>}
              </ul>
            )}
            {Object.keys(suppressions).length > 0 ? (
              <table className="trade-table" style={{ marginTop: 8, fontSize: "0.85em" }}>
                <thead>
                  <tr>
                    <th>Pair</th>
                    <th>What&apos;s benched</th>
                    <th>What this means</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(suppressions).map(([pair, sup]) => (
                    <tr key={pair}>
                      <td>{pair}</td>
                      <td>{suppressedLabel(sup)}</td>
                      <td style={{ fontSize: "0.85em", opacity: 0.85 }}>
                        {isSuppressed(sup, "gp_ensemble")
                          ? "New GP Brain entries blocked on this pair"
                          : isSuppressed(sup, "mean_reversion")
                            ? "New Mean Reversion entries blocked on this pair"
                            : "No style blocked"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="detail-muted">No pair-level suppressions active.</p>
            )}
            {(prioDisc === true || (Array.isArray(prioDisc) && prioDisc.length > 0)) && (
              <p className="cortex-flag">
                Priority discovery: several indicators are exiled — GP rediscovery
                should run sooner.
              </p>
            )}
            {(rollback === true || (Array.isArray(rollback) && rollback.length > 0)) && (
              <p className="cortex-flag cortex-flag-warn">
                Rollback flag: Mean Reversion win-rate is weak after enough trades —
                consider reviewing strategy params.
              </p>
            )}
            {policy.soft_weights && (
              <p className="cortex-flag" data-testid="soft-weights-flag">
                Soft weights ON — L35 benches shrink size instead of blocking new entries.
              </p>
            )}
          </div>
        )}

        {Object.keys(allocation).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16 }} data-testid="cortex-expert-weights">
            <div className="report-card-header">Expert weights (Layer A)</div>
            <Help>
              Per-pair size multipliers for each entry style. When{" "}
              <code>SOFT_WEIGHTS=1</code>, a style that would have been hard-benched
              still trades at a lower weight. Empty / all 100% usually means the flag
              is off or there is no suppress signal yet.
            </Help>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.85em" }}>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Mean Rev</th>
                  <th>Momentum</th>
                  <th>GP Brain</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(allocation).map(([pair, types]) => {
                  const cell = (etype) => {
                    const info = (types && types[etype]) || {};
                    const w = info.weight;
                    if (w == null) return "—";
                    const pct = `${Math.round(Number(w) * 100)}%`;
                    const soft = info.suppressed_soft;
                    return (
                      <span className={soft ? "dp-soft" : Number(w) < 0.999 ? "dp-probe" : "dp-full"}>
                        {pct}{soft ? " *" : ""}
                      </span>
                    );
                  };
                  return (
                    <tr key={pair}>
                      <td>{pair}</td>
                      <td>{cell("mean_reversion")}</td>
                      <td>{cell("rsi_momentum")}</td>
                      <td>{cell("gp_ensemble")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="detail-muted" style={{ marginTop: 8 }}>
              * = soft-suppress (would have been hard-blocked when SOFT_WEIGHTS is off)
            </p>
          </div>
        )}

        {Object.keys(byType).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16 }}>
            <div className="report-card-header">Performance by entry type</div>
            <Help>
              Compares how each way of entering a trade has done after closes.
              Higher WR + PnL → more likely to stay allowed; weak GP stats can
              trigger exile of individual indicators or a policy bench.
            </Help>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.85em" }}>
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Trades</th>
                  <th>Wins</th>
                  <th>WR</th>
                  <th>PnL</th>
                  <th>Live WR (router)</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(byType).map(([t, st]) => (
                  <tr key={t}>
                    <td>{fmtType(t)}</td>
                    <td>{st.n}</td>
                    <td>{st.wins}</td>
                    <td
                      className={
                        st.n > 0 && st.wins / st.n > 0.5 ? "pnl-positive" : "pnl-negative"
                      }
                    >
                      {st.n > 0 ? ((st.wins / st.n) * 100).toFixed(0) : 0}%
                    </td>
                    <td
                      className={
                        st.pnl > 0 ? "pnl-positive" : st.pnl < 0 ? "pnl-negative" : ""
                      }
                    >
                      {st.pnl > 0 ? "+" : ""}
                      {Number(st.pnl || 0).toFixed(2)}%
                    </td>
                    <td>
                      {typeWr[t] != null ? `${(typeWr[t] * 100).toFixed(0)}%` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {Object.keys(byPair).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16 }}>
            <div className="report-card-header">Per-pair totals</div>
            <Help>
              Same closed-trade scoreboard, sliced by currency pair. Use this to
              spot pairs that keep losing regardless of entry style.
            </Help>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.85em" }}>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Trades</th>
                  <th>Wins</th>
                  <th>WR</th>
                  <th>PnL</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(byPair).map(([p, st]) => (
                  <tr key={p}>
                    <td>{p}</td>
                    <td>{st.n}</td>
                    <td>{st.wins}</td>
                    <td
                      className={
                        st.n > 0 && st.wins / st.n > 0.5 ? "pnl-positive" : "pnl-negative"
                      }
                    >
                      {st.n > 0 ? ((st.wins / st.n) * 100).toFixed(0) : 0}%
                    </td>
                    <td
                      className={
                        st.pnl > 0 ? "pnl-positive" : st.pnl < 0 ? "pnl-negative" : ""
                      }
                    >
                      {st.pnl > 0 ? "+" : ""}
                      {Number(st.pnl || 0).toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {Object.keys(indicators).length > 0 && (
          <div className="report-card" style={{ marginBottom: 16, borderLeft: "3px solid #00aaff" }}>
            <div className="report-card-header" style={{ color: "#00aaff" }}>
              Per-indicator live results
            </div>
            <Help>
              Each row is a discovered GP formula. &quot;GP-Entry WR&quot; counts
              only times that indicator actually voted in a GP Brain entry. Below
              ~30% WR after enough attempts → exile (removed from the next vote).
            </Help>
            <table className="trade-table" style={{ marginTop: 4, fontSize: "0.82em" }}>
              <thead>
                <tr>
                  <th>Indicator</th>
                  <th>Entries</th>
                  <th>Wins</th>
                  <th>WR</th>
                  <th>PnL</th>
                  <th>GP-Entry WR</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(indicators).map(([name, ind]) => {
                  const gp = ind.by_type?.gp_ensemble || {};
                  const gpWr =
                    gp.entries > 0 ? ((gp.wins / gp.entries) * 100).toFixed(0) : "—";
                  const totalWr =
                    ind.entries > 0 ? ((ind.wins / ind.entries) * 100).toFixed(0) : 0;
                  return (
                    <tr key={name}>
                      <td
                        style={{
                          fontFamily: "monospace",
                          maxWidth: 280,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                        }}
                        title={name}
                      >
                        {name}
                      </td>
                      <td>{ind.entries}</td>
                      <td>{ind.wins}</td>
                      <td
                        className={
                          parseFloat(totalWr) > 50 ? "pnl-positive" : "pnl-negative"
                        }
                      >
                        {totalWr}%
                      </td>
                      <td className={ind.pnl > 0 ? "pnl-positive" : "pnl-negative"}>
                        {ind.pnl > 0 ? "+" : ""}
                        {Number(ind.pnl || 0).toFixed(2)}%
                      </td>
                      <td
                        className={
                          gpWr !== "—" && parseFloat(gpWr) > 30
                            ? "pnl-positive"
                            : "pnl-negative"
                        }
                      >
                        {gpWr === "—" ? "—" : `${gpWr}%`}
                      </td>
                      <td>{ind.exiled || exiled.includes(name) ? "🚫 exiled" : "active"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {exiled.length > 0 && (
          <div className="report-card" style={{ marginBottom: 16, borderLeft: "3px solid #ff4444" }}>
            <div className="report-card-header">
              <span style={{ color: "#ff4444" }}>Exiled indicators</span>{" "}
              <span style={{ opacity: 0.7 }}>({exiled.length})</span>
            </div>
            <Help>
              These formulas lost often enough that the GP Brain no longer lets
              them vote. They can return later if performance recovers after the
              exile cooldown.
            </Help>
            <table className="trade-table" style={{ marginTop: 8, fontSize: "0.85em" }}>
              <thead>
                <tr>
                  <th>Indicator</th>
                </tr>
              </thead>
              <tbody>
                {exiled.map((n, i) => (
                  <tr key={i}>
                    <td style={{ fontFamily: "monospace" }}>{n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {Object.keys(byType).length === 0 && Object.keys(indicators).length === 0 && (
          <div className="report-card">
            <Help>
              Cortex memory exists but has no closed outcomes yet. Once trades
              close, win-rates and exile decisions will appear here.
            </Help>
          </div>
        )}
      </div>
    </div>
  );
}
