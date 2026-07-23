import React, { useState, useEffect, useCallback, useMemo } from "react";
import { SkeletonDiscovered } from "./Skeleton.jsx";

/** Plain-language blurb under section headers (same pattern as Cortex). */
function Help({ children }) {
  return <p className="discovered-help">{children}</p>;
}

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

function wrQuality(ind) {
  const wr = Number(ind.win_rate || 0);
  if (wr >= 0.55) return { color: "#3fb950", label: "strong" };
  if (wr >= 0.45) return { color: "#d9b44a", label: "marginal" };
  if (wr > 0) return { color: "#e15a6d", label: "weak" };
  return { color: "#6e7681", label: ind.source === "discovered" ? "untested" : "seed" };
}

function liveFlagMeta(flag) {
  if (flag === "suppress") {
    return {
      cls: "gp-flag-suppress",
      label: "suppressed",
      title: "Live paper results are poor — entry engine skips this indicator (does not vote).",
    };
  }
  if (flag === "promote") {
    return {
      cls: "gp-flag-promote",
      label: "promoted",
      title: "Live paper results look good — entry prefers this indicator’s weight.",
    };
  }
  if (flag === "neutral" || flag === "pending") {
    return {
      cls: "gp-flag-neutral",
      label: flag,
      title: "Not enough live feedback yet, or results are mixed.",
    };
  }
  return null;
}

function signalLabel(signal) {
  if (signal > 0.3) return { cls: "bullish", text: "leans long" };
  if (signal < -0.3) return { cls: "bearish", text: "leans short" };
  return { cls: "neutral", text: "mixed / flat" };
}

function IndicatorRow({ ind }) {
  const wr = Number(ind.win_rate || 0);
  const histFit = Number(ind.fitness || 0);
  const liveFit = ind.live_fitness != null ? Number(ind.live_fitness) : null;
  const fit = liveFit != null && !Number.isNaN(liveFit) ? liveFit : histFit;
  const pnl = Number(ind.total_pnl || 0);
  const uses = Array.isArray(ind.uses) ? ind.uses : [];
  const q = wrQuality(ind);
  const flag = liveFlagMeta(ind.live_flag);
  const suppressed = ind.live_flag === "suppress";

  return (
    <li
      className={`gp-indicator${suppressed ? " gp-indicator-suppressed" : ""}`}
      data-bot={ind._bot}
    >
      <span className="gp-dot" style={{ background: q.color }} title={q.label} />
      <div className="gp-body">
        <div className="gp-name" title={ind.expr}>
          {ind.name || ind.expr || "—"}
        </div>
        <div className="gp-stats">
          <span className="gp-stat" title="Out-of-sample win rate on held-out candles (discovery math)">
            <b className={wr >= 0.55 ? "pc-up" : wr > 0 && wr < 0.45 ? "pc-down" : ""}>
              {wr > 0 ? `${(wr * 100).toFixed(1)}%` : "—"}
            </b>{" "}
            win
          </span>
          <span
            className="gp-stat"
            title={
              liveFit != null
                ? "Live-adjusted fitness (entry uses this when present)"
                : "Historical OOS correlation fitness"
            }
          >
            fit <b>{fit.toFixed(3)}</b>
            {liveFit != null && liveFit !== histFit ? (
              <span className="gp-fit-note"> live</span>
            ) : null}
          </span>
          <span className="gp-stat" title="Cumulative OOS PnL % from discovery evaluation">
            PnL{" "}
            <b className={pnl >= 0 ? "pc-up" : "pc-down"}>
              {pnl >= 0 ? "+" : ""}
              {pnl.toFixed(2)}%
            </b>
          </span>
          {ind.pool_lift != null && (
            <span className="gp-stat" title="Marginal lift to the indicator pool IC">
              lift <b>{Number(ind.pool_lift).toFixed(3)}</b>
            </span>
          )}
          {ind.complexity != null && (
            <span className="gp-stat" title="Expression tree complexity (nodes)">
              cx <b>{ind.complexity}</b>
            </span>
          )}
          {ind.island_id != null && (
            <span className="gp-meta-tag" title="Island that produced this elite">
              isl {ind.island_id}
            </span>
          )}
          {ind.niche?.behavior && (
            <span className="gp-meta-tag" title="Behavior niche from signal autocorrelation">
              {ind.niche.behavior}
            </span>
          )}
          {ind.niche?.complexity_bin && (
            <span className="gp-meta-tag" title="Complexity bin">
              {ind.niche.complexity_bin}
            </span>
          )}
          {ind.engine_version && (
            <span className="gp-meta-tag gp-engine" title={ind.run_id || ind.admit_reason || ""}>
              {String(ind.engine_version).replace("gp_v2_", "")}
            </span>
          )}
          {flag && (
            <span className={`gp-flag ${flag.cls}`} title={flag.title}>
              {flag.label}
            </span>
          )}
          {ind._shared_from && (
            <span
              className="gp-shared"
              title="Borrowed from a related pair (entry applies a shared penalty)"
            >
              shared ← {ind._shared_from}
            </span>
          )}
          {ind._bot && <span className="gp-bot-tag">{ind._bot}</span>}
          {ind.source && (
            <span className={`gp-src gp-src-${ind.source}`}>{ind.source}</span>
          )}
        </div>
        {uses.length > 0 && (
          <div className="gp-assets" title="Cross-asset drivers in the expression">
            {uses.map((a) => (
              <span key={a} className="gp-asset">
                <span className="gp-asset-dot" style={{ background: assetColor(a) }} />
                {a}
              </span>
            ))}
          </div>
        )}
        {suppressed && (
          <div className="gp-suppress-note">
            Not voting on live entries — fix live WR / wait for more samples, or let GP rediscover.
          </div>
        )}
      </div>
    </li>
  );
}

function DegCard({ pair, deg }) {
  if (!deg) return null;
  const { suppressed = 0, promoted = 0, shared = 0, weak_wr = 0, active = 0, total = 0 } = deg;
  return (
    <div className="deg-card">
      <div className="deg-card-pair">{pair}</div>
      <div className="deg-card-stats">
        <span title="Indicators that still vote">
          <b>{active}</b> active
        </span>
        <span title="live_flag=suppress — entry skips these">
          <b className={suppressed ? "pc-down" : ""}>{suppressed}</b> suppressed
        </span>
        <span title="live_flag=promote — preferred weight">
          <b className={promoted ? "pc-up" : ""}>{promoted}</b> promoted
        </span>
        <span title="OOS win rate under 45%">
          <b className={weak_wr ? "pc-down" : ""}>{weak_wr}</b> weak WR
        </span>
        <span title="Borrowed from a shared group pair">
          <b>{shared}</b> shared
        </span>
        <span>
          <b>{total}</b> total
        </span>
      </div>
    </div>
  );
}

function DiscoveryPulsePanel({ pulses, botFilter, pairBotMap }) {
  const entries = Object.entries(pulses || {}).filter(([pair, p]) => {
    if (botFilter === "all") return true;
    const owner =
      (p && p._bot) ||
      (pairBotMap && pairBotMap[pair]) ||
      null;
    if (owner) return owner === botFilter;
    // Unknown owner: hide on a specific bot tab (avoid BTC on Forex).
    return false;
  });
  if (!entries.length) return null;
  return (
    <div className="gp-pulse-panel" data-testid="discovery-pulse">
      <h3 className="discovered-section-title">Discovery run pulse</h3>
      <Help>
        Latest invent cycle stats for this bot: candidates evaluated, admit rate,
        MAP-Elites coverage, and best OOS. Updates when bots push after discovery.
      </Help>
      <div className="gp-pulse-grid">
        {entries
          .sort((a, b) => String(a[0]).localeCompare(b[0]))
          .map(([pair, p]) => {
            const cov = p?.map_elites?.coverage;
            const filled = p?.map_elites?.filled;
            const total = p?.map_elites?.total_cells;
            const regime =
              p?.interval && p?.horizon != null
                ? `${p.interval}/h${p.horizon}`
                : null;
            return (
              <div className="gp-pulse-card" key={`${p?._bot || "x"}:${pair}`}>
                <div className="gp-pulse-pair">
                  {pair}
                  {regime ? (
                    <span className="gp-pulse-regime" title="Invent candle TF / horizon">
                      {" "}
                      · {regime}
                    </span>
                  ) : null}
                </div>
                <div className="gp-pulse-stats">
                  <span title="Engine version">
                    <b>{String(p.engine_version || "—").replace("gp_v2_", "")}</b>
                  </span>
                  <span title="Unique candidates considered">
                    <b>{p.candidates_unique ?? "—"}</b> unique
                  </span>
                  <span title="Passed hard gates before admit">
                    <b>{p.candidates_gated ?? "—"}</b> gated
                  </span>
                  <span title="Admitted this run">
                    <b className={p.admitted ? "pc-up" : ""}>{p.admitted ?? 0}</b> admitted
                  </span>
                  <span title="Best OOS correlation among gated">
                    best <b>{p.best_oos != null ? Number(p.best_oos).toFixed(2) : "—"}</b>
                  </span>
                  <span title="MAP-Elites niche coverage">
                    niches{" "}
                    <b>
                      {filled != null && total != null
                        ? `${filled}/${total}`
                        : cov != null
                          ? `${(Number(cov) * 100).toFixed(0)}%`
                          : "—"}
                    </b>
                  </span>
                  <span title="ε-lexicase regime cases">
                    lex <b>{p.lexicase_cases ?? "—"}</b>
                  </span>
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}

function NicheMapPanel({ nicheMap, pairsFilter }) {
  const pairs = Object.keys(nicheMap || {}).filter((p) => {
    if (!pairsFilter) return true;
    return pairsFilter.includes(p);
  });
  if (!pairs.length) return null;
  // Aggregate counts across visible pairs.
  const agg = {};
  let totalCells = 27;
  for (const p of pairs) {
    const nm = nicheMap[p] || {};
    totalCells = nm.total_cells || totalCells;
    const counts = nm.counts || {};
    for (const [cell, n] of Object.entries(counts)) {
      agg[cell] = (agg[cell] || 0) + Number(n || 0);
    }
  }
  const cells = Object.keys(agg).sort();
  if (!cells.length) return null;
  const filled = cells.filter((c) => agg[c] > 0).length;
  return (
    <div className="gp-niche-panel" data-testid="niche-map">
      <h3 className="discovered-section-title">MAP-Elites niche map</h3>
      <Help>
        Behavior × complexity × horizon cells. Filled niches mean the archive has an elite
        there — ensemble voting prefers spreading across these.
      </Help>
      <p className="discovered-count">
        Coverage <b>{filled}</b> / {totalCells} cells
      </p>
      <div className="gp-niche-grid">
        {cells.map((cell) => {
          const n = agg[cell] || 0;
          const [behavior, cx, hz] = cell.split("|");
          return (
            <div
              key={cell}
              className={`gp-niche-cell${n ? " filled" : ""}`}
              title={`${cell}: ${n} indicator(s)`}
            >
              <span className="gp-niche-n">{n || "·"}</span>
              <span className="gp-niche-label">
                {behavior}
                <br />
                {cx} · {hz}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function DiscoveredView({ apiBase, isActive = true }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [botFilter, setBotFilter] = useState("all");

  const fetchData = useCallback(async () => {
    try {
      const r = await fetch(`${apiBase}/api/discovered`);
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

  const botNames = useMemo(() => {
    const fromBots = Object.keys(data?.bots || {});
    if (fromBots.length) return fromBots.sort();
    const s = new Set();
    Object.values(data?.pairs || {}).forEach((inds) => {
      (inds || []).forEach((i) => {
        if (i?._bot) s.add(i._bot);
      });
    });
    return [...s].sort();
  }, [data]);

  const filteredPairs = useMemo(() => {
    const pairs = data?.pairs || {};
    if (botFilter === "all") return pairs;
    const out = {};
    for (const [pair, inds] of Object.entries(pairs)) {
      const kept = (inds || []).filter((i) => i._bot === botFilter);
      if (kept.length) out[pair] = kept;
    }
    return out;
  }, [data, botFilter]);

  // Pair → owning bot (from indicator tags + known bot configs).
  const pairBotMap = useMemo(() => {
    const map = {
      "EUR/USD": "forex",
      "GBP/USD": "forex",
      "GBP/JPY": "forex",
      "AUD/USD": "forex",
      "XAU/USD": "gold",
      "XAG/USD": "gold",
      "BTC/USD": "crypto",
      "ETH/USD": "crypto",
    };
    for (const [pair, inds] of Object.entries(data?.pairs || {})) {
      for (const ind of inds || []) {
        if (ind?._bot) {
          map[pair] = ind._bot;
          break;
        }
      }
    }
    for (const [pair, pulse] of Object.entries(data?.discovery_pulse || {})) {
      if (pulse?._bot) map[pair] = pulse._bot;
    }
    return map;
  }, [data]);

  // Always show every known bot in the filter bar (including 0-indicator bots),
  // so crypto stays clickable after a rediscovery wipe.
  const tabBots = useMemo(() => {
    const s = new Set(botNames);
    Object.keys(data?.bots || {}).forEach((b) => s.add(b));
    return [...s].sort();
  }, [botNames, data]);

  if (loading) return <SkeletonDiscovered />;
  if (error) {
    return (
      <div className="discovered">
        <p className="error">
          {error} —{" "}
          <button className="retry-inline" onClick={fetchData}>
            retry
          </button>
        </p>
      </div>
    );
  }

  const globalTotal = data?.total_indicators || 0;
  const totalInd =
    botFilter === "all"
      ? globalTotal
      : Object.values(filteredPairs).reduce((n, inds) => n + inds.length, 0);
  const totalPairs = Object.keys(filteredPairs).length;

  // Truly empty catalog (no bot has indicators). Keep a single empty page.
  // Do NOT use this path for a bot filter with 0 rows — that hid the tabs and
  // looked like a broken/black page when clicking crypto/gold/forex.
  if (!data || globalTotal === 0) {
    return (
      <div className="discovered">
        <div className="discovered-head">
          <h2>Discovered</h2>
          <p className="discovered-subtitle">
            Genetic programming indicators the entry engine can vote with
          </p>
        </div>
        <div className="discovered-empty" data-testid="discovered-empty-global">
          <Help>
            No indicators yet. GP discovery runs on a schedule (and when the bot is stuck).
            Until a pair has admitted indicators, the{" "}
            <strong>GP Brain / gp_ensemble</strong> entry style has nothing to vote on for
            that pair — mean reversion and RSI can still trade. Check bot heartbeats and
            that <code>discovered/</code> state is being pushed on ingest.
          </Help>
        </div>
      </div>
    );
  }

  const ensemble = data.ensemble || {};
  const degradation = data.degradation || {};
  const sorted = Object.keys(filteredPairs).sort((a, b) => {
    const sa = Math.abs(ensemble[a]?.signal || 0);
    const sb = Math.abs(ensemble[b]?.signal || 0);
    if (sa !== sb) return sb - sa;
    return a.localeCompare(b);
  });

  const globalSuppressed = Object.values(filteredPairs).reduce(
    (n, inds) => n + inds.filter((i) => i.live_flag === "suppress").length,
    0,
  );
  const globalShared = Object.values(filteredPairs).reduce(
    (n, inds) => n + inds.filter((i) => i._shared_from).length,
    0,
  );

  return (
    <section className="discovered">
      <div className="discovered-head">
        <h2>Discovered</h2>
        <p className="discovered-subtitle">
          Genetic programming indicators the entry engine can vote with
        </p>
      </div>

      <div className="discovered-intro report-card">
        <Help>
          Each row is an admitted expression from GP discovery (OOS gates, permutation,
          walk-forward, MAP-Elites niches, pool-lift). The live <strong>gp_ensemble</strong> entry
          spreads votes across niches, skips <strong>suppressed</strong> indicators, and prefers
          <strong> live_fitness</strong> when present.
        </Help>
        <Help>
          <strong>What to do:</strong> many suppressed or weak-WR rows → expect fewer GP
          entries; wait for live feedback or a rediscovery cycle. Empty pair → GP Brain
          cannot open on that pair until discovery admits something.
        </Help>
      </div>

      {tabBots.length > 1 && (
        <div className="discovered-bot-tabs" role="tablist" aria-label="Filter by bot">
          <button
            type="button"
            role="tab"
            aria-selected={botFilter === "all"}
            className={`discovered-bot-tab${botFilter === "all" ? " active" : ""}`}
            onClick={() => setBotFilter("all")}
          >
            All
          </button>
          {tabBots.map((b) => (
            <button
              key={b}
              type="button"
              role="tab"
              aria-selected={botFilter === b}
              className={`discovered-bot-tab${botFilter === b ? " active" : ""}`}
              onClick={() => setBotFilter(b)}
            >
              {b}
              {data?.bots?.[b]?.total_indicators === 0 ? " (0)" : ""}
            </button>
          ))}
        </div>
      )}

      <div className="discovered-summary">
        <div className="discovered-stat">
          <span className="discovered-stat-num">{totalInd}</span>
          <span className="discovered-stat-label">indicators</span>
        </div>
        <div className="discovered-stat">
          <span className="discovered-stat-num">{totalPairs}</span>
          <span className="discovered-stat-label">pairs</span>
        </div>
        <div className="discovered-stat">
          <span className={`discovered-stat-num${globalSuppressed ? " pc-down" : ""}`}>
            {globalSuppressed}
          </span>
          <span className="discovered-stat-label">suppressed</span>
        </div>
        <div className="discovered-stat">
          <span className="discovered-stat-num">{globalShared}</span>
          <span className="discovered-stat-label">shared</span>
        </div>
      </div>

      <DiscoveryPulsePanel
        pulses={data.discovery_pulse || {}}
        botFilter={botFilter}
        pairBotMap={pairBotMap}
      />
      <NicheMapPanel
        nicheMap={data.niche_map || {}}
        pairsFilter={Object.keys(filteredPairs)}
      />

      {totalInd === 0 ? (
        <div className="discovered-empty" data-testid="discovered-empty-filter">
          <Help>
            No discovered indicators for <strong>{botFilter}</strong> yet. Traditional
            RSI / mean-reversion can still trade. GP Brain needs S10-approved formulas
            from discovery — check heartbeats and wait for the next invent cycle, or
            switch back to <strong>All</strong>.
          </Help>
        </div>
      ) : (
        <>
      <p className="discovered-count">
        <span className="discovered-legend">
          <span className="lg">
            <i style={{ background: "#3fb950" }} /> strong (≥55% WR)
          </span>
          <span className="lg">
            <i style={{ background: "#d9b44a" }} /> marginal
          </span>
          <span className="lg">
            <i style={{ background: "#e15a6d" }} /> weak (&lt;45%)
          </span>
          <span className="lg">
            <i style={{ background: "#6e7681" }} /> seed / untested
          </span>
        </span>
      </p>
      <Help>
        Dot color is <strong>out-of-sample win rate</strong> from discovery — how often the
        signal would have been right on held-out candles. Fitness is correlation strength;
        when a <em>live</em> tag appears, entry uses that adjusted score instead.
      </Help>

      <h3 className="discovered-section-title">Per pair</h3>
      <Help>
        The badge is a fitness×win-rate lean of <strong>non-suppressed</strong> indicators
        (same suppress rule as entry). It is a portfolio-quality hint, not the live z-score
        consensus that opens trades. Bullish / bearish thresholds are ±0.3.
      </Help>

      {sorted.map((pair) => {
        const inds = filteredPairs[pair] || [];
        if (!inds.length) return null;
        const ens = ensemble[pair] || {};
        // Recompute lean locally when filtered so UI matches visible rows.
        const active = inds.filter((i) => i.live_flag !== "suppress");
        let signal = ens.signal || 0;
        if (botFilter !== "all") {
          const tw = active.reduce((s, i) => {
            const f = Number(i.fitness || 0);
            const w = Number(i.win_rate || 0);
            return s + (f > 0 ? f * w : 0);
          }, 0);
          const bw = active.reduce((s, i) => {
            const f = Number(i.fitness || 0);
            const w = Number(i.win_rate || 0);
            return s + (w > 0.5 ? f * w : 0);
          }, 0);
          signal = tw > 0 ? (bw - (tw - bw)) / tw : 0;
        }
        const sig = signalLabel(signal);
        const strong = inds.filter((i) => Number(i.win_rate || 0) >= 0.55).length;
        const weak = inds.filter((i) => {
          const w = Number(i.win_rate || 0);
          return w > 0 && w < 0.45;
        }).length;
        const suppressed = inds.filter((i) => i.live_flag === "suppress").length;
        const multi = ens.multi_dim || active.filter((i) => (i.uses || []).length).length;

        return (
          <div className="discovered-pair" key={pair}>
            <div className="discovered-pair-head">
              <span className="discovered-pair-name">{pair}</span>
              <span
                className={`signal-badge signal-${sig.cls}`}
                title={`${sig.text} — fitness×WR lean of active indicators`}
              >
                {signal > 0 ? "+" : ""}
                {Number(signal).toFixed(3)}
                <span className="signal-hint">{sig.text}</span>
              </span>
              <span className="discovered-pair-meta">
                {inds.length} indicators
                {active.length !== inds.length && (
                  <span className="dq">{active.length} voting</span>
                )}
                {strong > 0 && <span className="dq dq-strong">{strong}★</span>}
                {weak > 0 && <span className="dq dq-weak">{weak}⚠</span>}
                {suppressed > 0 && (
                  <span className="dq dq-weak" title="Skipped by entry engine">
                    {suppressed} suppressed
                  </span>
                )}
                {multi ? <span className="dq">{multi} cross-asset</span> : null}
              </span>
            </div>
            <ul className="gp-indicators" data-testid="gp-indicators">
              {inds.map((ind, i) => (
                <IndicatorRow key={`${ind.name}-${ind.expr}-${i}`} ind={ind} />
              ))}
            </ul>
          </div>
        );
      })}

      {Object.keys(degradation).length > 0 && (
        <div className="discovered-deg">
          <h3 className="discovered-section-title">Health by pair</h3>
          <Help>
            Counts derived from the same indicator list entry uses:{" "}
            <strong>suppressed</strong> = live_flag suppress (no vote);{" "}
            <strong>promoted</strong> = live_flag promote; <strong>weak WR</strong> = OOS
            win rate under 45%; <strong>shared</strong> = borrowed from a group neighbor
            (entry applies a shared penalty). Act when suppressed ≫ active on a pair you
            expect GP Brain to trade.
          </Help>
          <div className="deg-grid">
            {sorted
              .filter((p) => degradation[p])
              .map((p) => (
                <DegCard key={p} pair={p} deg={degradation[p]} />
              ))}
          </div>
        </div>
      )}
        </>
      )}
    </section>
  );
}
