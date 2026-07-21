import React, { useState, useEffect, useCallback, useRef, lazy, Suspense } from "react";
import {
  XAxis, YAxis, Tooltip, Label,
  ResponsiveContainer, ReferenceLine, Area, AreaChart,
} from "recharts";
import { API_BASE } from "./config.js";
import { timeAgo, pairId } from "./utils.js";
import { COLORS } from "./colors.js";
import { SkeletonCard, SkeletonPortfolio, SkeletonActivity } from "./Skeleton.jsx";
import BotToggle from "./components/BotToggle.jsx";
import PipelineGap from "./components/PipelineGap.jsx";
import ReportsView from "./components/ReportsView.jsx";
import { useAuth, SetupScreen, LoginScreen } from "./components/Auth.jsx";

const DiscoveredView = lazy(() => import("./DiscoveredView.jsx"));
const AuditView = lazy(() => import("./AuditView.jsx"));
const CortexView = lazy(() => import("./CortexView.jsx"));

const TAB_KEYS = ["live", "activity", "reports", "discovered", "cortex", "audit", "alerts", "heartbeat", "flatline"];

// ───────────────────────────── Config ─────────────────────────────
const POLL_MS = 15000;

function getMarketCountdown(marketClosed) {
  const nowMs = Date.now();
  const d = new Date();
  const utcDay = d.getUTCDay();
  const sundayMs = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 22, 0, 0);
  const fridayMs = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 22, 0, 0);
  let target;

  if (marketClosed) {
    // Next Sunday 22:00 UTC
    const daysUntilSunday = (7 - utcDay) % 7;
    target = sundayMs + daysUntilSunday * 86400000;
    if (target <= nowMs) target += 7 * 86400000;
  } else {
    // Next Friday 22:00 UTC
    const daysUntilFriday = (5 - utcDay + 7) % 7;
    target = fridayMs + daysUntilFriday * 86400000;
    if (target <= nowMs) target += 7 * 86400000;
  }

  const diffMs = target - nowMs;
  if (diffMs <= 0) return null;

  const diffH = Math.floor(diffMs / 3600000);
  const diffM = Math.floor((diffMs % 3600000) / 60000);
  const diffS = Math.floor((diffMs % 60000) / 1000);

  // Sanity: if market is supposedly closed but reopen > 48h away, the flag is stale
  if (marketClosed) {
    if (diffH > 48) return null;  // stale flag — don't show anything
    return { text: `Reopens in ${diffH}h ${diffM}m ${diffS}s`, urgent: false };
  }
  if (diffH < 12) {
    return { text: `Closes in ${diffH}h ${diffM}m ${diffS}s`, urgent: diffH < 2 };
  }
  return null;
}


const PAIR_META = {
  // Forex
  "EUR/USD":  { bot: "forex", color: "#D4745C", timezone: "London/NY" },
  "GBP/USD":  { bot: "forex", color: "#c9a36a", timezone: "London/NY" },
  "AUD/USD":  { bot: "forex", color: "#5B7C99", timezone: "Sydney/Tokyo" },
  "GBP/JPY":  { bot: "forex", color: "#9B6B9E", timezone: "London/Tokyo" },
  // Commodities
  "XAU/USD":  { bot: "gold", color: "#D4AF37", timezone: "24h" },
  "XAG/USD":  { bot: "gold", color: "#C0C0C0", timezone: "24h" },
  // Crypto
  "BTC/USD":  { bot: "crypto", color: "#F7931A", timezone: "24h" },
  "ETH/USD":  { bot: "crypto", color: "#627EEA", timezone: "24h" },
};

// ───────────────────────── Learning glossary ─────────────────────────

const GLOSSARY = {
  rsi: {
    term: "RSI (Relative Strength Index)",
    plain: "A 0-100 score measuring whether a pair has been bought or sold too aggressively recently. Low values (under 30-45) mean \"oversold\" — possibly due for a bounce. High values (above 70) mean \"overbought.\"",
    analogy: "Like a rubber band — the further it's stretched, the more likely it snaps back.",
  },
  atr: {
    term: "ATR (Average True Range)",
    plain: "Measures how much a price typically moves in a given period. Used to set stop-losses that adapt to volatility — wider stops when the market is wild, tighter when calm.",
    analogy: "Like giving a sprinter more room on a windy day than a calm one.",
  },
  regime: {
    term: "Market Regime",
    plain: "A label for the overall trend: BULL (trending up), BEAR (trending down), or NEUTRAL (mixed). The bot sizes positions smaller in NEUTRAL and skips BEAR entirely.",
    analogy: "Like checking the weather before deciding how big a coat to wear.",
  },
  adx: {
    term: "ADX (Average Directional Index)",
    plain: "Measures how STRONG a trend is, regardless of direction. Low ADX means the market is range-bound and choppy with no clear trend.",
    analogy: "A speedometer for trend strength, not direction.",
  },
  bollinger: {
    term: "Bollinger Bands",
    plain: "Three lines around the price: a middle average and an upper/lower band based on volatility. The mean reversion strategy buys when price touches the lower band, betting it drifts back to the middle.",
    analogy: "A price 'comfort zone' — when price strays too far outside it, it tends to get pulled back in.",
  },
  quality_score: {
    term: "Entry Quality Score (0-10)",
    plain: "A composite score combining signal strength, market regime, volume confirmation, and other factors. Higher = more conditions aligned in the bot's favor.",
    analogy: "A pre-flight checklist — more boxes checked, more confidence in takeoff.",
  },
  sharpe: {
    term: "Sharpe Ratio",
    plain: "Risk-adjusted return. Above 1.0 is solid, above 2.0 is excellent. A strategy with less profit but far less risk can beat one with more profit and wild swings.",
    analogy: "Two drivers reach the same destination — one weaving through traffic, one calm in lane. Same arrival, very different Sharpe.",
  },
  mr: {
    term: "Mean Reversion",
    plain: "A strategy based on the idea that prices tend to return to their average over time. When price drops far below average (touching the lower Bollinger Band), it bets on a bounce back up.",
    analogy: "A boomerang — throw it far away, expect it to come back.",
  },
  rsi_mom: {
    term: "RSI Momentum",
    plain: "Buys when RSI drops below a threshold (e.g. 42-48) with negative rate of change, confirming short-term exhaustion in a downtrend — expecting a snap-back rally.",
    analogy: "Catching a falling knife — but only when you've seen the knife hit the floor first.",
  },
  volume: {
    term: "Volume Filter",
    plain: "Checks if the current trading volume is above average. Low volume means few traders are active — signals during quiet periods are less reliable, so the bot skips.",
    analogy: "Like checking crowd size before trusting a rumor — an empty room's gossip is less trustworthy.",
  },
  chart: {
    term: "Chart Vision",
    plain: "The bot takes a screenshot of a price chart and uses AI to read it — detecting trend direction, support/resistance levels, and patterns a human would see. If the chart says 'downtrend,' lower-quality entries are blocked.",
    analogy: "Like having an experienced trader glance at a chart and say 'nah, not yet' before the bot enters.",
  },
  session: {
    term: "Trading Session",
    plain: "Forex trades in overlapping global sessions (Sydney, Tokyo, London, New York). The mean reversion bot prefers London/NY overlap when volume and volatility are highest.",
    analogy: "Like choosing rush hour to drive — more cars, but also more predictable patterns.",
  },
  gp_ensemble: {
    term: "GP Ensemble (Genetic Programming)",
    plain: "An AI system that automatically discovers new trading indicators by evolving mathematical formulas — like breeding the best predictors. Runs weekly. Indicators with poor performance get 'exiled' (temporarily disabled).",
    analogy: "Like a scientist running thousands of experiments and only keeping the ones that actually work.",
  },
  exile: {
    term: "Exiled Indicator",
    plain: "A GP-discovered indicator that performed poorly (losing trades or low win rate) and has been temporarily disabled. It can return after a cooldown if conditions improve.",
    analogy: "A player benched after a bad game — they can come back, but they need to prove themselves first.",
  },
  signal: {
    term: "Signal Strength",
    plain: "How strongly the bot's indicators suggest a trade. Positive = bullish (buy), negative = bearish (sell), near zero = no clear direction. The bot only enters when signals cross a confidence threshold.",
    analogy: "Like a volume knob — quiet signals are ignored, only loud and clear ones get attention.",
  },
  stop_loss: {
    term: "Stop Loss",
    plain: "An automatic exit if the trade goes against you. The bot sets a stop as a percentage below entry price. If hit, the trade closes with a small loss to prevent a bigger one. This is why you see S: values in exit stats.",
    analogy: "A fire escape — you hope you never use it, but you always know exactly where it is.",
  },
};

// ───────────────────────────── Helpers ─────────────────────────────

function safeVal(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") {
    if ("score" in v) return v.score;
    if ("value" in v) return v.value;
    return JSON.stringify(v);
  }
  return v;
}

function fmtPct(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const arrow = n > 0 ? "▲ " : n < 0 ? "▼ " : "";
  const sign = n > 0 ? "+" : "";
  return `${arrow}${sign}${Number(n).toFixed(2)}%`;
}

function fmtPrice(n, pair) {
  if (n === null || n === undefined) return "—";
  // Metals + USDT pairs are large numbers -> show 2 decimals with thousands sep.
  if (pair?.includes("XAU") || pair?.includes("XAG") || pair?.includes("USDT"))
    return `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  if (pair?.includes("JPY")) return Number(n).toFixed(3);
  if (pair?.includes("/USD")) return `$${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return Number(n).toFixed(5);
}

function ThemeToggle() {
  const [dark, setDark] = useState(() => {
    const saved = localStorage.getItem("hermes_theme");
    return saved !== "light";
  });

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    localStorage.setItem("hermes_theme", dark ? "dark" : "light");
  }, [dark]);

  return (
    <button className="theme-toggle" onClick={() => setDark((d) => !d)} title={dark ? "Switch to light mode" : "Switch to dark mode"}>
      {dark ? "☀" : "☾"}
    </button>
  );
}

function holdTime(cycles) {
  if (!cycles) return "—";
  const mins = cycles;
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function humanizeSkip(reason) {
  const map = {
    volume_below_avg: "low volume",
    bear_regime: "bear market",
    chart_downtrend_quality_filter: "downtrend, quality too low",
    chart_hard_block: "chart says avoid/downtrend",
    reentry_cooldown: "re-entry cooldown (stop-loss)",
    adx_below_threshold: "no trend (ranging)",
    mr_no_signal: "conditions not aligned",
    fear_greed_high: "extreme greed",
    funding_rate_high: "overleveraged longs",
    quality_below_min: "quality too low",
  };
  return map[reason] || reason || "no signal yet";
}

function skipToGlossary(reason) {
  const map = {
    volume_below_avg: "atr",
    bear_regime: "regime",
    chart_downtrend_quality_filter: "quality_score",
    chart_hard_block: "quality_score",
    reentry_cooldown: "quality_score",
    adx_below_threshold: "adx",
    mr_no_signal: "bollinger",
    fear_greed_high: "regime",
    funding_rate_high: "regime",
    quality_below_min: "quality_score",
  };
  return map[reason] || "rsi";
}

function sessionStatus() {
  const h = new Date().getUTCHours();
  const sessions = [];
  if (h >= 22 || h < 7) sessions.push("Sydney/Tokyo");
  if (h >= 0 && h < 9) sessions.push("Tokyo");
  if (h >= 7 && h < 13) sessions.push("London");
  if (h >= 13 && h < 17) sessions.push("London/NY Overlap");
  if (h >= 13 && h < 22) sessions.push("New York");
  if (h >= 21 || h < 2) sessions.push("Late NY");
  return sessions;
}

function regimeColor(regime) {
  if (regime === "BULL") return COLORS.up;
  if (regime === "BEAR") return COLORS.down;
  return COLORS.neutral;
}

// ───────────────────────── Glossary tooltip ─────────────────────────

function GlossaryTerm({ id, children }) {
  const [open, setOpen] = useState(false);
  const g = GLOSSARY[id];
  if (!g) return <>{children}</>;
  return (
    <span
      className="gterm"
      tabIndex={0}
      role="button"
      aria-expanded={open}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); } }}
      onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
    >
      {children}
      <span className="gdot">?</span>
      {open && (
        <div className="gpop" onClick={(e) => e.stopPropagation()}>
          <div className="gpop-title">{g.term}</div>
          <div className="gpop-body">{g.plain}</div>
          <div className="gpop-analogy">{g.analogy}</div>
        </div>
      )}
    </span>
  );
}

// ───────────────────────── Filter chain trace ─────────────────────────

function FilterChain({ strategy_type, lastSkip }) {
  const chains = {
    rsi_momentum: ["RSI", "Volume", "Regime", "Quality", "Chart"],
    mean_reversion: ["Bollinger", "RSI", "ADX", "Session"],
  };
  const steps = chains[strategy_type] || chains.rsi_momentum;

  const stepFailMap = {
    volume_below_avg: "Volume",
    bear_regime: "Regime",
    chart_downtrend_quality_filter: "Chart",
    adx_below_threshold: "ADX",
    mr_no_signal: "Bollinger",
    fear_greed_high: "Regime",
    funding_rate_high: "Regime",
    quality_below_min: "Quality",
  };
  const failedLabel = lastSkip ? stepFailMap[lastSkip.reason_skipped] : null;
  const failIdx = steps.indexOf(failedLabel);

  return (
    <div className="filterchain">
      {steps.map((s, i) => {
        const isFailed = i === failIdx;
        const isPastFail = failIdx >= 0 && i < failIdx;
        return (
          <React.Fragment key={s}>
            <div
              className={`fc-dot ${isFailed ? "fc-fail" : isPastFail || failIdx < 0 ? "fc-pass" : "fc-idle"}`}
              title={s}
            >
              <span className="fc-label">{s}</span>
            </div>
            {i < steps.length - 1 && <div className="fc-line" />}
          </React.Fragment>
        );
      })}
    </div>
  );
}

function botHasPipelineGap(botName, overview) {
  const bot = overview?.bots?.[botName];
  if (!bot) return true;
  const hasActivity =
    (bot.recent_trades?.length || 0) + (bot.recent_skips?.length || 0) > 0;
  const hasHeartbeat = Boolean(bot._received_at || bot.heartbeat?.cycle);
  return !hasActivity && !hasHeartbeat;
}

// ───────────────────────── Pair card ─────────────────────────

function PairCard({ pair, data, strategy, regime, onSelect, isSelected, botPaused, livePrice }) {
  const meta = PAIR_META[pair] || {};
  const trades = data?.trades || [];
  const openTrade = trades.find((t) => t.exit_reason == null || t.exit_reason === undefined || t.exit_reason === "");
  const lastClosed = [...trades].reverse().find((t) => t.exit_reason);
  const lastSkip = data?.lastSkip;

  const pnl = openTrade?._unrealised_pct ?? openTrade?.pnl_pct ?? openTrade?.unrealised_pct ?? null;
  const isUp = pnl !== null && pnl > 0;

  const strategyLabel = strategy === "mean_reversion" ? "Mean Reversion" : "RSI Momentum";
  const sessions = meta?.timezone === "24h" ? ["24h"] : sessionStatus();
  const [fresh, setFresh] = useState(false);
  useEffect(() => {
    if (lastClosed) { setFresh(true); const t = setTimeout(() => setFresh(false), 4000); return () => clearTimeout(t); }
  }, [lastClosed?.pnl_pct]);

  // ── Sparkline (mini price chart) ──
  const [sparkPrices, setSparkPrices] = useState(null);
  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/spark?pair=${encodeURIComponent(pair)}`)
      .then(r => r.json())
      .then(d => { if (!cancelled && d.prices?.length >= 2) setSparkPrices(d.prices); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [pair]);

  const sparkLine = sparkPrices ? (() => {
    const w = 72, h = 22, pad = 1;
    const mn = Math.min(...sparkPrices), mx = Math.max(...sparkPrices), rng = mx - mn || 1;
    const pts = sparkPrices.map((p, i) => `${(i/(sparkPrices.length-1))*w},${h-((p-mn)/rng)*(h-pad*2)-pad}`).join(" ");
    const clr = sparkPrices[sparkPrices.length-1] >= sparkPrices[0] ? COLORS.up : COLORS.down;
    return { pts, clr };
  })() : null;

  // ── Entry flash ──
  const [entryFlash, setEntryFlash] = useState(false);
  const prevId = useRef(null);
  useEffect(() => {
    if (openTrade && openTrade.id && openTrade.id !== prevId.current) {
      prevId.current = openTrade.id;
      setEntryFlash(true);
      const t = setTimeout(() => setEntryFlash(false), 4000);
      return () => clearTimeout(t);
    }
  }, [openTrade?.id]);

  return (
    <div
      className={`pair-card ${openTrade ? "pair-card-in-trade" : ""} ${entryFlash ? "pair-card-flash" : ""} ${isSelected ? "pair-card-sel" : ""} ${botPaused ? "pair-card-disabled" : ""}`}
      data-testid="pair-card"
      data-bot={meta.bot}
      role="button"
      tabIndex={0}
      aria-label={`${pair}${openTrade ? ", in position" : ""}${botPaused ? ", paused" : ""}`}
      aria-pressed={isSelected}
      onClick={() => onSelect(pair)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(pair); } }}
      style={{ "--accent": meta.color }}
    >
      {botPaused && <span className="pair-disabled-badge">Paused</span>}
      {openTrade && <span className="pc-trade-badge-abs" title="In position" />}
      <div className="pc-head">
        <div className="pc-pair-group">
          <span className="pc-pair">{pair}</span>
          <span className="pc-session-badge" title={`Active: ${sessions.join(", ")}`}>
            {sessions.length > 0 ? "🟢" : "⚪"} {sessions[0] || "Off"}
          </span>
        </div>
        <div className="pc-strategies">
          <span className={`pc-strategy pc-strategy-${strategy}`}>{strategyLabel}</span>
          {openTrade?.entry_type === "gp_ensemble" && (
            <span className="pc-strategy pc-strategy-gp_ensemble" title="Entry generated by the GP genetic-programming brain (paper)">GP Brain</span>
          )}
        </div>
      </div>

      <div className="pc-status">
        {openTrade ? (
          <>
            <span className={`pc-pnl ${isUp ? "pc-up" : "pc-down"}`}>{fmtPct(pnl)}</span>
            <span className="pc-held">{holdTime(openTrade.hold_cycles)}</span>
          </>
        ) : (
          <span className="pc-watching">Watching</span>
        )}
      </div>

      <div className="pc-indicators">
        <span className="pc-ind-label">Regime</span>
        <span className="pc-ind-val" style={{ color: regimeColor(regime) }}>{regime || "—"}</span>
        <span className="pc-ind-label">Price</span>
        <span className="pc-ind-val pc-price">{livePrice !== undefined && livePrice !== null ? fmtPrice(livePrice, pair) : "—"}</span>
      </div>

      <FilterChain strategy_type={strategy} lastSkip={!openTrade ? lastSkip : null} />

      {lastSkip && !openTrade && livePrice === undefined && (
        <div className="pc-skip-reason">
          Blocked by:{" "}
          <GlossaryTerm id={skipToGlossary(lastSkip.reason_skipped)}>
            {humanizeSkip(lastSkip.reason_skipped)}
          </GlossaryTerm>
        </div>
      )}

      {sparkLine && (
        <div className="pc-spark" onClick={(e) => { e.stopPropagation(); onSelect(pair); }}>
          <svg width={72} height={22} viewBox="0 0 72 22">
            <polyline fill="none" stroke={sparkLine.clr} strokeWidth="1.5" points={sparkLine.pts} />
          </svg>
        </div>
      )}

      {lastClosed && (
        <div className={`pc-last-closed ${fresh ? "pc-last-closed-fresh" : ""}`}>
          Last:{" "}
          <span className={lastClosed.pnl_pct > 0 ? "pc-up" : "pc-down"}>
            {fmtPct(lastClosed.pnl_pct)}
          </span>{" "}
          ({lastClosed.exit_reason})
        </div>
      )}
    </div>
  );
}

// ───────────────────────── SparkChart (detail panel) ─────────────────────────

function SparkChart({ pair, meta }) {
  const [prices, setPrices] = useState(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let dead = false;
    setPrices(null); setErr(false);
    fetch(`${API_BASE}/api/spark?pair=${encodeURIComponent(pair)}`)
      .then(r => r.json()).then(d => { if (!dead) { if (d.prices?.length >= 2) setPrices(d.prices); else setErr(true); } })
      .catch(() => { if (!dead) setErr(true); });
    return () => { dead = true; };
  }, [pair]);
  if (err) return null;
  if (!prices) return <div className="detail-spark-loading">Loading price chart...</div>;
  const mn = Math.min(...prices), mx = Math.max(...prices), mid = (mn + mx) / 2;
  const dir = prices.at(-1) >= prices[0] ? "Up ▲" : "Down ▼";
  const chg = ((prices.at(-1) - prices[0]) / prices[0] * 100).toFixed(2);
  const chartData = prices.map((p, i) => ({ i: i + 1, p }));
  return (
    <div className="detail-spark">
      <div className="dc-label">Price (5m · last {prices.length}) — {dir} {chg}%</div>
      <ResponsiveContainer width="100%" height={150}>
        <AreaChart data={chartData}>
          <defs>
            <linearGradient id={`sg${pairId(pair)}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={meta?.color||"#888"} stopOpacity={0.35} />
              <stop offset="100%" stopColor={meta?.color||"#888"} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="i" hide />
          <YAxis hide domain={["auto","auto"]} />
          <ReferenceLine y={mid} stroke={COLORS.chartGrid} strokeDasharray="3 3" />
          <Tooltip contentStyle={{background:COLORS.chartBg,border:`1px solid ${COLORS.chartBorder}`,borderRadius:8,fontSize:11}} formatter={(v) => [`${Number(v).toFixed(5)}`]} labelFormatter={()=>""} />
          <Area type="monotone" dataKey="p" stroke={meta?.color||"#888"} fill={`url(#sg${pairId(pair)})`} strokeWidth={2} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
      <div className="spark-explain">
        <p><strong>Direction:</strong> {dir} · {chg}% over {prices.length} candles.</p>
        <p>The line is colored <strong className="pc-up">green</strong> if the last candle is higher than the first (net upward). <strong style={{color:COLORS.down}}>Red</strong> means net downward. This is the overall direction — a green line can have dips in the middle.</p>

        <details>
          <summary>📖 What am I looking at?</summary>
          <p>This chart shows the <strong>closing price</strong> of {pair} for each 5-minute candle. Each dot on the line is one price point — the final price traders agreed on for that 5-minute window.</p>
          <p>Think of it like a <strong>heartbeat monitor</strong> for the market. When the line is rising, buyers are in control. When it's falling, sellers are pushing price down. Flat lines mean indecision — no one is sure which way to go.</p>
        </details>

        <details>
          <summary>🤖 How does the bot use this?</summary>
          <p>The bot reads every single one of these price points to make decisions:</p>
          <ul>
            <li><strong>Mean Reversion (EUR/USD, GBP/USD, GBP/JPY):</strong> Waits for price to dip toward the bottom Bollinger Band. When price touches it and RSI is low, the bot buys, expecting price to snap back toward the middle.</li>
            <li><strong>RSI Momentum (AUD/USD):</strong> Waits for RSI to drop below its threshold with negative momentum. The bot buys expecting the downtrend to exhaust.</li>
          </ul>
        </details>

        <details>
          <summary>🔢 The numbers</summary>
          <ul>
            <li><strong>Lowest:</strong> {mn.toFixed(5)} &nbsp; <strong>Highest:</strong> {mx.toFixed(5)}</li>
            <li><strong>Net change:</strong> {chg}%</li>
            <li><strong>Candles shown:</strong> {prices.length} × 5m = ~{Math.round(prices.length * 5 / 60)} hours</li>
            <li><strong>Dashed line:</strong> Midpoint — quick visual for top/bottom half of range.</li>
          </ul>
        </details>

        <details>
          <summary>⚠️ When to worry</summary>
          <ul>
            <li><strong>Flat line:</strong> Market is closed (weekend). Stale-data guard blocks entries.</li>
            <li><strong>Sharp drops below BB lower:</strong> MR buy could get stopped out quickly. Tight ATR stop limits damage to ~0.3%.</li>
            <li><strong>No candles updating:</strong> yfinance data could be stale. Bot's same-candle guard skips cycles.</li>
          </ul>
        </details>

        <details>
          <summary>🎯 What to watch for</summary>
          <p>If price dips toward the bottom of its range while RSI drops into the 40s, a trade may be near. Check the <strong>Filter Chain</strong> on the pair card.</p>
        </details>
      </div>
    </div>
  );
}

// ───────────────────────── Detail panel ─────────────────────────

function DetailPanel({ pair, botData, strategyParams }) {
  const [maximized, setMaximized] = useState(false);
  const [versions, setVersions] = useState(null);

  useEffect(() => {
    if (!pair) return;
    const bot = PAIR_META[pair]?.bot || "forex";
    fetch(`${API_BASE}/api/per-version/${bot}?pair=${encodeURIComponent(pair)}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => setVersions(d?.versions || null))
      .catch(() => setVersions(null));
  }, [pair]);

  if (!pair) {
    return (
      <div className="detail-empty">
        <div className="detail-empty-mark">↑</div>
        <p>Click any pair to see the full story — what the bot is watching, why it acted (or didn't), current strategy parameters, and what each number means. Click the expand button to get a full-screen detailed report.</p>
      </div>
    );
  }

  const meta = PAIR_META[pair];
  const trades = (botData?.recent_trades || []).filter((t) => (t.pair || t.asset) === pair);
  const openTrades = (botData?.recent_open_trades || []).filter((t) => (t.asset || t.pair) === pair);
  const allPairTrades = [...openTrades.map(t => ({ ...t, exit_reason: null })), ...trades];
  const openTrade = allPairTrades.find((t) => !t.exit_reason);
  const closedTrades = allPairTrades.filter((t) => t.exit_reason).slice(-10).reverse();
  const strategy = strategyParams?.[pair] || {};
  const strategyLabel = strategy.strategy_type === "mean_reversion" ? "Mean Reversion" : "RSI Momentum";

  const chartData = closedTrades
    .slice()
    .reverse()
    .reduce((acc, t) => {
      const prevCum = acc.length ? acc[acc.length - 1].cum : 0;
      acc.push({ idx: acc.length + 1, pnl: t.pnl_pct, cum: prevCum + (t.pnl_pct || 0) });
      return acc;
    }, []);

  const pnls = closedTrades.map((t) => t.pnl_pct || 0);
  const avgPnl = pnls.length ? pnls.reduce((a, b) => a + b, 0) / pnls.length : 0;
  const stdPnl = pnls.length >= 2 ? Math.sqrt(pnls.reduce((s, p) => s + (p - avgPnl) ** 2, 0) / (pnls.length - 1)) : 0;
  const sharpe = stdPnl > 0 ? (avgPnl / stdPnl).toFixed(2) : "—";
  const maxDD = pnls.length ? Math.min(...pnls).toFixed(2) : "—";

  return (
    <>
      <div className={`detail-panel ${maximized ? "detail-panel-maximized" : ""}`}>
        <button
          className="detail-max-btn"
          onClick={() => setMaximized(!maximized)}
          title={maximized ? "Minimize" : "Maximize to full report"}
        >
          {maximized ? "⊡" : "⊞"}
        </button>

        <div className="detail-title">
          <h3>{pair}</h3>
          <span className={`pc-strategy pc-strategy-${strategy.strategy_type}`}>{strategyLabel}</span>
          {openTrade?.entry_type === "gp_ensemble" && (
            <span className="pc-strategy pc-strategy-gp_ensemble" title="Entry generated by the GP genetic-programming brain (paper)">GP Brain</span>
          )}
        </div>

        <div className="detail-params">
          <div className="dc-label">Strategy Parameters</div>
          <div className="params-grid">
            <div className="param-item">
              <span className="param-label"><GlossaryTerm id="rsi">RSI Threshold</GlossaryTerm></span>
              <span className="param-val">{strategy.entry?.threshold || strategy.entry?.mr_entry_rsi || "—"}</span>
            </div>
            <div className="param-item">
              <span className="param-label">Stop Loss</span>
              <span className="param-val">{strategy.stop_loss_pct}%</span>
            </div>
            <div className="param-item">
              <span className="param-label">Profit Target</span>
              <span className="param-val">{strategy.profit_target_pct}%</span>
            </div>
            <div className="param-item">
              <span className="param-label">Position Size</span>
              <span className="param-val">{strategy.position_size_r}</span>
            </div>
            <div className="param-item">
              <span className="param-label">ATR Multiplier</span>
              <span className="param-val">{strategy.atr_multiplier}×</span>
            </div>
            <div className="param-item">
              <span className="param-label">Time Exit</span>
              <span className="param-val">{strategy.time_exit_cycles}c ({holdTime(strategy.time_exit_cycles)})</span>
            </div>
            <div className="param-item">
              <span className="param-label">ADX Threshold</span>
              <span className="param-val">{strategy.adx_threshold}</span>
            </div>
            <div className="param-item">
              <span className="param-label">Version</span>
              <span className="param-val">v{strategy.version}</span>
            </div>
          </div>
        </div>

        {versions && versions.length > 0 && (
          <div className="detail-versions">
            <div className="dc-label">Version History (this pair)</div>
            <div className="versions-mini">
              <div className="vm-header">
                <span className="vm-cell vm-ver">Ver</span>
                <span className="vm-cell vm-trades">Trades</span>
                <span className="vm-cell vm-wr">WR</span>
                <span className="vm-cell vm-pnl">PnL</span>
                <span className="vm-cell vm-exits">S/T/X</span>
                <span className="vm-cell vm-trend" />
              </div>
              {versions.map(v => (
                <div key={v.version} className={`vm-row ${v.trend === "declined" ? "vm-declined" : v.trend === "improved" ? "vm-improved" : ""}`}>
                  <span className="vm-cell vm-ver">v{v.version}</span>
                  <span className="vm-cell vm-trades">{v.trades}</span>
                  <span className={`vm-cell vm-wr ${v.win_rate >= 50 ? "pc-up" : "pc-down"}`}>
                    {v.win_rate}%{v.low_confidence ? <span className="vm-lc" title="Low confidence — fewer than 10 trades">*</span> : ""}
                    {v.wr_lower !== undefined && <span className="vm-ci"> ({v.wr_lower}–{v.wr_upper}%)</span>}
                  </span>
                  <span className={`vm-cell vm-pnl ${v.total_pnl >= 0 ? "pc-up" : "pc-down"}`}>{v.total_pnl >= 0 ? "+" : ""}{v.total_pnl}%</span>
                  <span className="vm-cell vm-exits">{v.stops}/{v.targets}/{v.timeouts}</span>
                  <span className="vm-cell vm-trend">{v.trend === "improved" ? "▲" : v.trend === "declined" ? "▼" : "—"}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {openTrade ? (
          <div className="detail-position">
            <div className="dc-label">Open Position</div>
            <div className="dp-row">
              <span>Entry</span>
              <span className="mono">{fmtPrice(openTrade.entry_price, pair)}</span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="atr">Stop loss</GlossaryTerm></span>
              <span className="mono dp-stop">
                {fmtPrice(openTrade._stop_price ?? openTrade.stop_price, pair)}
              </span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="rsi">RSI at entry</GlossaryTerm></span>
              <span className="mono">{safeVal(openTrade.entry_rsi)}</span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="regime">Regime at entry</GlossaryTerm></span>
              <span className="mono">{safeVal(openTrade.entry_regime)}</span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="quality_score">Quality score</GlossaryTerm></span>
              <span className="mono">{safeVal(openTrade.entry_quality_score)}/10</span>
            </div>
            {openTrade.entry_type === "gp_ensemble" && (
              <div className="dp-row">
                <span>Engine</span>
                <span className="mono dp-gp">GP Brain · paper</span>
              </div>
            )}
            <div className="dp-row">
              <span>Held</span>
              <span className="mono">{holdTime(openTrade.hold_cycles)}</span>
            </div>
            {(openTrade._unrealised_pct ?? openTrade.unrealised_pct) !== undefined && (
              <div className="dp-row">
                <span>Unrealised P&L</span>
                <span className={`mono ${(openTrade._unrealised_pct ?? openTrade.unrealised_pct) >= 0 ? "pc-up" : "pc-down"}`}>
                  {fmtPct(openTrade._unrealised_pct ?? openTrade.unrealised_pct)}
                </span>
              </div>
            )}
            {openTrade.chart_context && (
              <div className="dp-chart-note">
                <span className="dp-chart-label">Chart read at entry</span>
                <p>{openTrade.chart_context}</p>
              </div>
            )}
          </div>
        ) : closedTrades.length > 0 ? (
          <div className="detail-position">
            <div className="dc-label">Last Trade Entry</div>
            <div className="dp-row">
              <span>Entry</span>
              <span className="mono">{fmtPrice(closedTrades[0].entry_price, pair)}</span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="rsi">RSI at entry</GlossaryTerm></span>
              <span className="mono">{safeVal(closedTrades[0].entry_rsi)}</span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="regime">Regime at entry</GlossaryTerm></span>
              <span className="mono">{safeVal(closedTrades[0].entry_regime)}</span>
            </div>
            <div className="dp-row">
              <span><GlossaryTerm id="quality_score">Quality score</GlossaryTerm></span>
              <span className="mono">{safeVal(closedTrades[0].entry_quality_score)}/10</span>
            </div>
            <div className="dp-row">
              <span>Exit</span>
              <span className="mono">{closedTrades[0].exit_reason} · {fmtPct(closedTrades[0].pnl_pct)}</span>
            </div>
            {closedTrades[0].chart_context && (
              <div className="dp-chart-note">
                <span className="dp-chart-label">Chart read at entry</span>
                <p>{closedTrades[0].chart_context}</p>
              </div>
            )}
          </div>
        ) : (
          <p className="detail-muted">No open position — the bot is watching for the next signal.</p>
        )}

        {closedTrades.length > 0 && (
          <div className="detail-perf">
            <div className="dc-label">Performance (last {closedTrades.length} closed)</div>
            <div className="perf-stats">
              <div className="perf-stat">
                <span className="perf-label"><GlossaryTerm id="sharpe">Sharpe</GlossaryTerm></span>
                <span className="perf-val">{sharpe}</span>
              </div>
              <div className="perf-stat">
                <span className="perf-label">Max Single Loss</span>
                <span className="perf-val pc-down">{maxDD}%</span>
              </div>
              <div className="perf-stat">
                <span className="perf-label">Avg PnL</span>
                <span className={`perf-val ${avgPnl >= 0 ? "pc-up" : "pc-down"}`}>{fmtPct(avgPnl)}</span>
              </div>
              <div className="perf-stat">
                <span className="perf-label">Win Rate</span>
                <span className="perf-val">
                  {pnls.length ? ((pnls.filter(p => p > 0).length / pnls.length) * 100).toFixed(0) : 0}%
                </span>
              </div>
            </div>
          </div>
        )}

        {chartData.length > 1 && (
          <div className="detail-chart">
            <div className="dc-label">Cumulative P&L — last {chartData.length} closed trades</div>
            <ResponsiveContainer width="100%" height={140}>
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id={`cumGrad-${pairId(pair)}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={meta?.color} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={meta?.color} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="idx" hide />
                <YAxis hide domain={["auto", "auto"]} />
                <ReferenceLine y={0} stroke={COLORS.chartGrid} strokeDasharray="3 3" />
                <Tooltip
                  contentStyle={{ background: COLORS.chartBg, border: `1px solid ${COLORS.chartBorder}`, borderRadius: 8, fontSize: 12 }}
                  formatter={(v) => [`${v.toFixed(2)}%`, "Cumulative"]}
                  labelFormatter={() => ""}
                />
                <Area type="monotone" dataKey="cum" stroke={meta?.color} fill={`url(#cumGrad-${pairId(pair)})`} strokeWidth={2} />
                {chartData.length > 0 && chartData[chartData.length - 1].cum !== undefined && (
                  <ReferenceLine
                    x={chartData[chartData.length - 1].idx}
                    stroke="none"
                    label={<Label value={`${chartData[chartData.length - 1].cum >= 0 ? "+" : ""}${chartData[chartData.length - 1].cum.toFixed(2)}%`} position="right" fill={COLORS.ghostStrong || "#A8AFBD"} fontSize={11} />}
                  />
                )}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}

        <SparkChart pair={pair} meta={meta} />

        <div className="detail-recent">
          <div className="dc-label">Recent closed trades</div>
          {closedTrades.length === 0 && <p className="detail-muted">No closed trades yet for this pair.</p>}
          {closedTrades.map((t, i) => (
            <div className="dr-row" key={i}>
              <span className={t.pnl_pct > 0 ? "pc-up" : "pc-down"}>{fmtPct(t.pnl_pct)}</span>
              <span className="dr-reason">{t.exit_reason}</span>
              <span className="dr-held">{holdTime(t.hold_cycles)}</span>
            </div>
          ))}
        </div>
      </div>

      {maximized && (
        <DetailFullscreen
          pair={pair}
          botName={PAIR_META[pair]?.bot || "forex"}
          onClose={() => setMaximized(false)}
        />
      )}
    </>
  );
}

// ───────────────────────── Fullscreen Detail Report ─────────────────────────

function DetailFullscreen({ pair, botName, onClose }) {
  const [report, setReport] = useState("");
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [pairCopied, setPairCopied] = useState(false);

  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const fetchReport = useCallback(async () => {
    setLoading(true);
    try {
      const pairKey = pair.replace("/", "_");
      const res = await fetch(`${API_BASE}/api/export-text/pair/${pairKey}`);
      const text = await res.text();
      setReport(text);
    } catch (e) {
      setReport(`Error: ${e.message}`);
    }
    setLoading(false);
  }, [API_BASE, pair]);

  useEffect(() => { fetchReport(); }, [fetchReport]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(report);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch (e) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    }
  };

  const handleCopyPair = async () => {
    try {
      await navigator.clipboard.writeText(report);
      setPairCopied(true);
      setTimeout(() => setPairCopied(false), 2500);
    } catch (e) {
      setPairCopied(true);
      setTimeout(() => setPairCopied(false), 2500);
    }
  };

  return (
    <div className="detail-fullscreen-overlay" onClick={onClose}>
      <div className="detail-fullscreen" onClick={(e) => e.stopPropagation()}>
        <div className="dfs-header">
          <div className="dfs-title-group">
            <h2>{pair} — Detailed Report</h2>
            <span className="dfs-subtitle">Full per-pair analysis — lifetime, recent, current state</span>
          </div>
          <button className="dfs-close-btn" onClick={onClose} title="Close (Esc)">×</button>
        </div>

        <div className="dfs-controls">
          <button className="dfs-btn dfs-btn-primary" onClick={fetchReport} disabled={loading}>
            {loading ? "Loading…" : "Generate Report"}
          </button>
          <button className={`dfs-btn ${copied ? "dfs-btn-copied" : ""}`} onClick={handleCopy} disabled={!report}>
            {copied ? "Copied ✓" : "Copy Report"}
          </button>
          <button className={`dfs-btn ${pairCopied ? "dfs-btn-copied" : ""}`} onClick={handleCopyPair}>
            {pairCopied ? "Copied ✓" : "Copy Pair Analysis"}
          </button>
        </div>

        <div className="dfs-report-body">
          {loading && !report ? (
            <div className="dfs-loading">Generating report…</div>
          ) : (
            <pre className="dfs-report-text">{report}</pre>
          )}
        </div>
      </div>
    </div>
  );
}

// ───────────────────────── Skip Analysis ─────────────────────────

function SkipAnalysis({ apiBase, botName }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${apiBase}/api/skip-analysis/${botName}`);
      if (res.ok) setData(await res.json());
    } catch (e) { /* silent */ }
    setLoading(false);
  }, [apiBase, botName]);

  useEffect(() => { load(); const id = setInterval(load, 30000); const onVis = () => { if (!document.hidden) load(); }; document.addEventListener("visibilitychange", onVis); return () => { clearInterval(id); document.removeEventListener("visibilitychange", onVis); }; }, [load]);

  if (loading && !data) return <SkeletonActivity rows={6} />;
  if (!data || data.total_skips === 0) return <div className="detail-muted">No skips recorded yet — the bot hasn't been blocked.</div>;

  return (
    <div className="skip-analysis">
      <div className="dc-label">Skip Analysis (last 200 signals) — {data.total_skips} total</div>
      {Object.entries(data.by_pair).map(([pair, d]) => (
        <div className="sa-pair" key={pair}>
          <div className="sa-pair-head">
            <span className="sa-pair-name">{pair}</span>
            <span className="sa-pair-count">{d.total} skips</span>
            {d.missed_pnl_count > 0 && (
              <span className={`sa-pair-missed ${d.missed_pnl_sum >= 0 ? "pc-up" : "pc-down"}`}>
                missed avg: {fmtPct(d.missed_pnl_sum / d.missed_pnl_count)}
              </span>
            )}
          </div>
          <div className="sa-reasons">
            {Object.entries(d.reasons).sort((a, b) => b[1] - a[1]).map(([reason, count]) => (
              <div className="sa-reason" key={reason}>
                <span className="sa-reason-label">
                  <GlossaryTerm id={skipToGlossary(reason)}>{humanizeSkip(reason)}</GlossaryTerm>
                </span>
                <div className="sa-bar-wrap">
                  <div className="sa-bar" style={{ width: `${(count / d.total) * 100}%` }} />
                </div>
                <span className="sa-reason-count">{count}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ───────────────────────── Activity feed ─────────────────────────

function ActivityFeed({ overview }) {
  const events = [];

  for (const [botName, bot] of Object.entries(overview?.bots || {})) {
    for (const t of bot.recent_trades || []) {
      if (t.exit_reason) {
        events.push({
          ts: t.exit_ts,
          type: "close",
          text: `${t.pair || t.asset} closed ${fmtPct(t.pnl_pct)} (${t.exit_reason})`,
          good: t.pnl_pct > 0,
        });
      } else if (t.entry_ts) {
        events.push({
          ts: t.entry_ts,
          type: "entry",
          text: `${t.pair || t.asset} entered at ${fmtPrice(t.entry_price, t.pair)} — quality ${safeVal(t.entry_quality_score ?? t.quality ?? "?")}/10`,
          good: null,
        });
      }
    }
    for (const h of bot.recent_hypotheses || []) {
      events.push({
        ts: h.ts,
        type: "reflect",
        text: `Reflection (${botName}): ${h.variable} ${h.old_value} → ${h.new_value}`,
        good: null,
      });
    }
  }

  events.sort((a, b) => new Date(b.ts) - new Date(a.ts));

  return (
    <div className="feed">
      <div className="feed-title">Activity</div>
      {events.length === 0 && <p className="detail-muted">No activity yet — waiting on the bots.</p>}
      {events.slice(0, 25).map((e, i) => (
        <div className={`feed-row feed-${e.type}`} key={i}>
          <span className="feed-time">{timeAgo(e.ts)}</span>
          <span
            className={`feed-dot feed-dot-${e.type} ${e.good === true ? "feed-good" : e.good === false ? "feed-bad" : ""}`}
          />
          <span className="feed-text">{e.text}</span>
        </div>
      ))}
    </div>
  );
}

// ───────────────────────── Portfolio pulse ─────────────────────────

function PortfolioPulse({ overview }) {
  let totalPnl = 0;
  let openCount = 0;
  let closedCount = 0;
  let gpOpenCount = 0;

  for (const bot of Object.values(overview?.bots || {})) {
    // Closed trades: only rows in trades.jsonl that carry an exit_reason.
    // (Open-log rows there have no exit_reason and would double-count the
    // live positions already in recent_open_trades — so we ignore them here.)
    for (const t of bot.recent_trades || []) {
      if (t.exit_reason) closedCount++;
    }
    // Open positions: recent_open_trades is the authoritative live-open list.
    for (const t of bot.recent_open_trades || []) {
      openCount++;
      totalPnl += t._unrealised_pct ?? t.unrealised_pct ?? t.pnl_pct ?? 0;
      if (t.entry_type === "gp_ensemble") gpOpenCount++;
    }
  }

  const avgPnl = openCount ? totalPnl / openCount : 0;

  return (
    <div className="pulse" role="status" aria-live="polite" aria-label={`Portfolio: ${openCount} open positions, average ${fmtPct(avgPnl)}`}>
      <div className="pulse-main">
        <div className="pulse-label">Portfolio pulse</div>
        <div className={`pulse-num ${avgPnl >= 0 ? "pc-up" : "pc-down"}`}>{fmtPct(avgPnl)}</div>
        <div className="pulse-sub">avg across {openCount} open position{openCount === 1 ? "" : "s"}</div>
      </div>
      <div className="pulse-stats">
        <div className="pulse-stat">
          <span className="ps-num">{openCount}</span>
          <span className="ps-label">open</span>
        </div>
        <div className="pulse-stat">
          <span className="ps-num">{closedCount}</span>
          <span className="ps-label">closed</span>
        </div>
        {gpOpenCount > 0 && (
          <div className="pulse-stat pulse-stat-gp">
            <span className="ps-num">{gpOpenCount}</span>
            <span className="ps-label">GP brain</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ───────────────────────── Chat assistant ─────────────────────────

function ChatPopup() {
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState("");
  const [chat, setChat] = useState([]);
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef(null);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [chat]);

  const send = async () => {
    const q = msg.trim();
    if (!q || busy || q.length > 500) return;
    setMsg("");
    setChat((p) => [...p, { who: "user", text: q }]);
    setBusy(true);
    try {
      const token = localStorage.getItem("hermes_token");
      const headers = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const r = await fetch(`${API_BASE}/api/chat`, {
        method: "POST", headers,
        body: JSON.stringify({ question: q }),
      });
      const d = await r.json();
      setChat((p) => [...p, { who: "bot", text: d.answer }]);
    } catch { setChat((p) => [...p, { who: "bot", text: "Sorry, couldn't reach the server." }]); }
    setBusy(false);
  };

  return (
    <>
      <button className="chat-fab" onClick={() => setOpen((o) => !o)}>{open ? "×" : "💬"}</button>
      {open && (
        <div className="chat-panel">
          <div className="chat-header">Ask the bot</div>
          <div className="chat-body">
            {chat.length === 0 && <p className="chat-empty">Ask anything — trade duration, strategy, performance…</p>}
            {chat.map((c, i) => <div key={i} className={`chat-bubble ${c.who}`}>{c.text}</div>)}
            <div ref={bottomRef} />
          </div>
          <div className="chat-input-row">
            <input className="chat-input" value={msg} onChange={(e) => setMsg(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()} placeholder="Ask about your trades..." disabled={busy} />
            <button className="chat-send" onClick={send} disabled={busy || !msg.trim()}>{busy ? "..." : "→"}</button>
          </div>
        </div>
      )}
    </>
  );
}

// ───────────────────────── Alerts ─────────────────────────

function useAlerts(apiBase) {
  const [alerts, setAlerts] = useState([]);
  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/api/alerts`);
      const json = await res.json();
      setAlerts(json.alerts || []);
    } catch (e) { /* silent */ }
  }, [apiBase]);
  useEffect(() => { refresh(); const id = setInterval(refresh, 30000); return () => clearInterval(id); }, [refresh]);
  const dismiss = useCallback(async (key) => {
    setAlerts((prev) => prev.filter((a) => a.key !== key));
    try { await fetch(`${apiBase}/api/alerts/dismiss`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) }); }
    catch (e) { refresh(); }
  }, [apiBase, refresh]);
  return { alerts, dismiss };
}

function AlertBanner({ alerts, dismiss, onViewAll }) {
  if (!alerts.length) return null;
  const top = alerts[0];
  const extra = alerts.length - 1;
  return (
    <div className="alert-banner">
      <span className="alert-banner-dot" />
      <div className="alert-banner-body">
        <span className="alert-banner-title">{top.title}</span>
        <span className="alert-banner-detail">{top.detail}</span>
      </div>
      {extra > 0 && <button className="alert-banner-more" onClick={onViewAll}>+{extra} more</button>}
      <button className="alert-banner-dismiss" onClick={() => dismiss(top.key)} title="Dismiss">×</button>
    </div>
  );
}

function AlertsView({ alerts, dismiss }) {
  if (!alerts.length) {
    return (
      <div className="alerts-empty">
        <div className="detail-empty-mark">✓</div>
        <p>No active alerts. The dashboard watches for patterns like a pair losing every trade for a day and will surface them here the moment they happen.</p>
      </div>
    );
  }
  return (
    <div className="alerts-list">
      {alerts.map((a) => (
        <div className={`alert-row alert-row-${a.severity}`} key={a.key}>
          <div className="alert-row-main">
            <span className="alert-row-title">{a.title}</span>
            <span className="alert-row-detail">{a.detail}</span>
          </div>
          <div className="alert-row-meta">
            <span className="alert-row-bot">{a.bot}</span>
            <button className="alert-row-dismiss" onClick={() => dismiss(a.key)}>Dismiss</button>
          </div>
        </div>
      ))}
    </div>
  );
}

// ───────────────────────── Onboarding ─────────────────────────

function OnboardingBanner() {
  const [visible, setVisible] = useState(() => localStorage.getItem("hermes_onboarded") !== "1");

  if (!visible) return null;

  const dismiss = () => {
    localStorage.setItem("hermes_onboarded", "1");
    setVisible(false);
  };

  return (
    <div className="onboard-banner" role="dialog" aria-label="Welcome to Hermes">
      <div className="onboard-content">
        <h2>👋 Welcome to Hermes</h2>
        <p>This is your self-improving paper trading dashboard. Each pair card shows what the bot is watching — click one for full details.</p>
        <p>Look for the <span className="gdot-inline">?</span> icons — click them anytime to learn what a trading term means. No prior knowledge needed.</p>
        <div className="onboard-tabs">
          <span><strong>Live</strong> — active trades & strategy</span>
          <span><strong>Activity</strong> — trade feed & version history</span>
          <span><strong>Reports</strong> — daily, lifetime & custom range</span>
          <span><strong>Cortex</strong> — AI decision engine internals</span>
          <span><strong>Audit</strong> — code/risk self-review</span>
        </div>
        <button className="onboard-dismiss" onClick={dismiss}>Got it — take me to the dashboard →</button>
      </div>
    </div>
  );
}

// ── Per-Version View (tab content with copy) ──
function VersionView({ perVersion }) {
  const botNames = { forex: "Forex Bot", gold: "Gold Bot", crypto: "Crypto Bot" };
  const [versionText, setVersionText] = useState(null);
  const [copied, setCopied] = useState(false);

  const generateVersionText = useCallback(() => {
    const lines = ["VERSION PERFORMANCE SUMMARY"];
    if (perVersion) {
      Object.entries(perVersion).forEach(([bot, versions]) => {
        if (!versions || !versions.length) return;
        lines.push("", `--- ${botNames[bot] || bot} ---`);
        versions.forEach(v => {
          const avg = v.avg_pnl ? (v.avg_pnl >= 0 ? "+" : "") + v.avg_pnl + "%" : "0%";
          const tot = v.total_pnl ? (v.total_pnl >= 0 ? "+" : "") + v.total_pnl + "%" : "0%";
          const trend = v.trend === "improved" ? " ▲" : v.trend === "declined" ? " ▼" : "";
          lines.push(`  v${v.version}: ${v.trades} trades | ${v.win_rate}% WR | ${tot} total | S:${v.stops} T:${v.targets} X:${v.timeouts}${trend}`);
        });
      });
    }
    return lines.join("\n");
  }, [perVersion]);

  const handleCopy = useCallback(() => {
    const text = generateVersionText();
    setVersionText(text);
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }).catch(() => {});
    }
  }, [generateVersionText]);

  if (!perVersion || Object.keys(perVersion).length === 0) {
    return (
      <div className="version-placeholder">
        No version data yet. Trades will generate version comparisons after reflection cycles.
      </div>
    );
  }

  return (
    <div className="version-tab-content">
      <div className="version-tab-head">
        <span className="dc-label">Per-version performance across all bots</span>
        <button className="copy-btn" onClick={handleCopy}>
          {copied ? "Copied ✓" : "Copy version analysis"}
        </button>
      </div>

      {versionText && (
        <div className="export-preview">
          <pre>{versionText}</pre>
        </div>
      )}

      <div className="version-cards-grid">
        {Object.entries(perVersion).map(([bot, versions]) => (
          <div key={bot} className="version-card">
            <h3 className="version-bot-name">
              {botNames[bot] || bot} ({versions.length} versions)
            </h3>
            <div className="version-grid">
              {versions.map(v => (
                <div
                  key={v.version}
                  className={`version-item${v.trend === "declined" ? " version-declined" : v.trend === "improved" ? " version-improved" : ""}`}
                >
                  <div className="version-header">
                    <span className="version-badge">v{v.version}</span>
                    <span className="version-trend">
                      {v.trend === "improved" ? "▲" : v.trend === "declined" ? "▼" : ""}
                    </span>
                  </div>
                  <div className="version-stats">
                    <div><strong>{v.trades}</strong> trades</div>
                    <div>
                      <span className={v.win_rate >= 50 ? "positive" : "negative"}>
                        {v.win_rate}% WR
                      </span>
                    </div>
                    <div className={v.total_pnl >= 0 ? "positive" : "negative"}>
                      {v.total_pnl >= 0 ? "+" : ""}{v.total_pnl}% PnL
                    </div>
                    <div className="version-detail">
                      avg {v.avg_pnl >= 0 ? "+" : ""}{v.avg_pnl}%
                    </div>
                    <div className="version-detail">
                      S:{v.stops} T:{v.targets} X:{v.timeouts}
                    </div>
                  </div>

                  {v.pair_breakdown && v.pair_breakdown.length > 1 && (
                    <div className="version-pair-rows">
                      {v.pair_breakdown.map(pb => (
                        <div key={pb.pair} className="version-pair-row">
                          <span className="version-pair-name">{pb.pair}</span>
                          <span className="version-pair-stat">
                            <span className="version-pair-label">trades</span>
                            {pb.trades}
                          </span>
                          <span className="version-pair-stat">
                            <span className="version-pair-label">WR</span>
                            <span className={pb.win_rate >= 50 ? "positive" : "negative"}>
                              {pb.win_rate}%
                            </span>
                          </span>
                          <span className="version-pair-stat">
                            <span className="version-pair-label">PnL</span>
                            <span className={pb.total_pnl >= 0 ? "positive" : "negative"}>
                              {pb.total_pnl >= 0 ? "+" : ""}{pb.total_pnl}%
                            </span>
                          </span>
                          <span className="version-pair-stat">
                            <span className="version-pair-label">S/T/X</span>
                            {pb.stops}/{pb.targets}/{pb.timeouts}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  {v.pairs && v.pairs.length > 0 && (
                    <div className="version-pairs">{v.pairs.join(", ")}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ───────────────────────── Heartbeat + Flatline (restored from original) ─────────────────────────

function useJson(url) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let dead = false;
    fetch(url).then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { if (!dead) { setData(d); setError(null); } })
      .catch(e => { if (!dead) setError(e.message); });
    return () => { dead = true; };
  }, [url]);
  return { data, error };
}

function HeartbeatView({ apiBase }) {
  const bots = ["forex", "gold", "crypto"];
  return (
    <div className="heartbeat-view">
      <div className="dc-label">Heartbeat — last-sent bot state (signal, regime, cycle)</div>
      <div className="hb-grid">
        {bots.map(bot => {
          const { data, error } = useJson(`${apiBase}/api/heartbeat/${bot}`);
          return (
            <div className="hb-card" key={bot}>
              <div className="hb-bot">{bot}</div>
              {error && <div className="detail-muted">error: {error}</div>}
              {!error && !data && <div className="detail-muted">loading…</div>}
              {data && (
                <pre className="hb-json">{JSON.stringify(data, null, 2)}</pre>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FlatlineView({ apiBase }) {
  const bots = ["forex", "gold", "crypto"];
  return (
    <div className="flatline-view">
      <div className="dc-label">Flatline — pairs the bot stopped trading (stale signal / no entries)</div>
      {bots.map(bot => {
        const { data, error } = useJson(`${apiBase}/api/flatline/${bot}`);
        return (
          <div className="fl-bot" key={bot}>
            <div className="fl-bot-head">{bot}</div>
            {error && <div className="detail-muted">error: {error}</div>}
            {!error && !data && <div className="detail-muted">loading…</div>}
            {data && (Array.isArray(data) && data.length === 0
              ? <div className="detail-muted">no flatlined pairs</div>
              : Array.isArray(data) ? (
              <ul className="fl-log">
                  {data.slice(-50).reverse().map((row, i) => (
                    <li key={i}>{JSON.stringify(row)}</li>
                  ))}
                </ul>
              ) : <div className="detail-muted">no flatlined pairs</div>)}
          </div>
        );
      })}
    </div>
  );
}

// ───────────────────────────── App ─────────────────────────────

export default function App() {
  const { mode, setup, login, logout } = useAuth();
  const [overview, setOverview] = useState(null);
  const [strategyParams, setStrategyParams] = useState(null);
  const [selectedPair, setSelectedPair] = useState(null);
  const [error, setError] = useState(null);
  const [lastFetch, setLastFetch] = useState(null);
  const [view, setView] = useState("live");
  const [subTab, setSubTab] = useState("activity");
  const { alerts, dismiss } = useAlerts(API_BASE);
  const [botStatus, setBotStatus] = useState({});
  const [perVersion, setPerVersion] = useState({});

  const detailRef = useRef(null);

  const handleSelectPair = (pair) => {
    setSelectedPair(pair);
    setTimeout(() => {
      detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 100);
  };

  const scrollToTop = () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const fetchOverview = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/overview`);
      if (!res.ok) throw new Error(`API returned ${res.status}`);
      const json = await res.json();
      setOverview(json);
      setLastFetch(new Date());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const fetchStrategyParams = useCallback(async () => {
    try {
      const merged = { pairs: {} };
      for (const bot of ["forex", "gold", "crypto"]) {
        const res = await fetch(`${API_BASE}/api/strategy-params/${bot}`);
        if (res.ok) {
          const data = await res.json();
          if (data?.pairs) Object.assign(merged.pairs, data.pairs);
        }
      }
      setStrategyParams(merged);
    } catch (e) { /* silent */ }
  }, []);

  const fetchBotStatus = useCallback(async () => {
    const status = {};
    for (const bot of ["forex", "gold", "crypto"]) {
      try {
        const res = await fetch(`${API_BASE}/api/bot/${bot}/pulse`);
        if (res.ok) {
          const json = await res.json();
          status[bot] = json.desired_state;
        }
      } catch (e) { /* silent */ }
    }
    setBotStatus(status);
  }, []);

  const fetchPerVersion = useCallback(async () => {
    const data = {};
    for (const bot of ["forex", "gold", "crypto"]) {
      try { const r = await fetch(`${API_BASE}/api/per-version/${bot}`); if (r.ok) data[bot] = (await r.json()).versions || []; }
      catch (e) { /* silent */ }
    }
    setPerVersion(data);
  }, []);

  useEffect(() => {
    const isLiveTab = view === "live" || view === "activity";
    if (isLiveTab) fetchOverview();
    if (view === "live") fetchStrategyParams();
    if (view === "activity") fetchPerVersion();
    fetchBotStatus(); // lightweight — always needed for toggle buttons

    const id = setInterval(() => {
      if (isLiveTab) fetchOverview();
      if (view === "live") fetchStrategyParams();
      if (view === "activity") fetchPerVersion();
      fetchBotStatus();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [fetchOverview, fetchStrategyParams, fetchBotStatus, fetchPerVersion, view]);

  // ── Force refresh when tab regains visibility/focus (browser throttles bg polling) ──
  useEffect(() => {
    const refreshActive = () => {
      if (view === "live" || view === "activity") {
        fetchOverview(); fetchStrategyParams(); fetchBotStatus();
      }
    };
    document.addEventListener("visibilitychange", refreshActive);
    window.addEventListener("focus", refreshActive);
    document.addEventListener("pageshow", refreshActive);
    return () => {
      document.removeEventListener("visibilitychange", refreshActive);
      window.removeEventListener("focus", refreshActive);
      document.removeEventListener("pageshow", refreshActive);
    };
  }, [fetchOverview, fetchStrategyParams, fetchBotStatus, view]);

  // ── Keyboard shortcuts ──
  useEffect(() => {
    const handler = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      const idx = parseInt(e.key);
      if (idx >= 1 && idx <= TAB_KEYS.length) { setView(TAB_KEYS[idx - 1]); return; }
      if (e.key === "Escape") { setSelectedPair(null); return; }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const pairData = {};
  const liveRegimes = {};
  const livePrices = {};
  for (const bot of Object.values(overview?.bots || {})) {
    const regimes = bot?.live_indicators?.regimes || bot?.heartbeat?.regimes || {};
    Object.assign(liveRegimes, regimes);
    // Per-pair live price snapshot (bot pushes this each cycle via heartbeat).
    const prices = bot?.prices || bot?.heartbeat?.prices || {};
    Object.assign(livePrices, prices);
      }
      const marketClosed = Object.values(overview?.bots || {}).some(
        (bot) => bot?.live_indicators?.market_closed || bot?.heartbeat?.market_closed
      );
      for (const [botName, bot] of Object.entries(overview?.bots || {})) {
    const trades = bot.recent_trades || [];
    const skips = bot.recent_skips || [];
    const openTrades = bot.recent_open_trades || [];
    for (const pair of Object.keys(PAIR_META)) {
      if (PAIR_META[pair].bot !== botName) continue;
      const pTrades = trades.filter((t) => (t.pair || t.asset) === pair);
      const pOpen = openTrades.filter((t) => (t.asset || t.pair) === pair);
      const pSkips = skips.filter((s) => s.pair === pair);
      const allTrades = [...pOpen.map(t => ({ ...t, exit_reason: null })), ...pTrades];
      pairData[pair] = { trades: allTrades, lastSkip: pSkips[pSkips.length - 1] };
    }
  }

  const selectedBotName = selectedPair ? PAIR_META[selectedPair]?.bot : null;
  const selectedBotData = selectedBotName ? overview?.bots?.[selectedBotName] : null;

  if (mode === "loading") return <div className="auth-screen"><div className="auth-spinner" /></div>;
  if (mode === "setup") return <SetupScreen onSetup={setup} />;
  if (mode === "login") return <LoginScreen onLogin={login} />;

  return (
    <div className="app" role="main">
      <header className="app-header">
        <div>
          <h1>Hermes</h1>
          <p className="app-sub">self-improving trading system — live monitor</p>
        </div>
        <div className="app-status">
          <div className="view-toggle" role="tablist" aria-label="Dashboard sections">
            {["live","activity","reports","discovered","cortex","audit","alerts","heartbeat","flatline"].map((tab) => {
              const label = tab.charAt(0).toUpperCase() + tab.slice(1);
              const isActive = view === tab;
              return (
                <button
                  key={tab}
                  className={`vtab ${isActive ? "vtab-active" : ""}`}
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => setView(tab)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setView(tab); } }}
                >
                  {tab === "alerts" ? (
                    <>Alerts{alerts.length > 0 ? <span className="vtab-badge">{alerts.length}</span> : null}</>
                  ) : label}
                </button>
              );
            })}
          </div>
          {error ? (
            <span className="status-bad">connection error — <button className="retry-inline" onClick={fetchOverview}>retry</button></span>
          ) : (
            <span className="status-ok">● live · updated {lastFetch ? timeAgo(lastFetch.toISOString()) : "—"}</span>
          )}
          <button className="auth-signout" onClick={logout}>Sign out</button>
          <ThemeToggle />
        </div>
      </header>

      {view !== "alerts" && <OnboardingBanner />}
      {view !== "alerts" && <AlertBanner alerts={alerts} dismiss={dismiss} onViewAll={() => setView("alerts")} />}

      {view === "live" ? (
        <>
          {marketClosed && (() => {
              const cd = getMarketCountdown(true);
              return (
                <div className="market-closed-banner">
                  <span>⏸</span> Forex market closed — bot is waiting for next session
                  {cd && <span className="market-countdown">{cd.text}</span>}
                </div>
              );
            })()}

            {!marketClosed && (() => {
              const cd = getMarketCountdown(false);
              if (!cd) return null;
              return (
                <div className={`market-closed-banner ${cd.urgent ? "market-closing-soon" : ""}`}>
                  <span>⏳</span> {cd.text}
                </div>
              );
            })()}

          {!overview ? <SkeletonPortfolio /> : <PortfolioPulse overview={overview} />}

          <section className="bot-section">
            <BotToggle botName="forex" label="Foreign Exchange" staleDays={overview?.bots?.forex?.live_indicators?.discovery_stale_days} />
            <div className="cards-grid" role="list" aria-label="Forex pairs">
              {!overview ? (
                Array.from({ length: 4 }).map((_, i) => <SkeletonCard key={i} />)
              ) : (
                Object.entries(PAIR_META).filter(([,m]) => m.bot === "forex").map(([pair]) => (
                <PairCard
                  key={pair}
                  pair={pair}
                  data={pairData[pair]}
                  strategy={strategyParams?.pairs?.[pair]?.strategy_type || "rsi_momentum"}
                  regime={liveRegimes[pair] || "—"}
                  livePrice={livePrices[pair]}
                  onSelect={handleSelectPair}
                  isSelected={selectedPair === pair}
                  botPaused={botStatus.forex === "paused"}
                />
              )))}
            </div>
          </section>

          <section className="bot-section">
            <BotToggle botName="gold" label="Gold" staleDays={overview?.bots?.gold?.live_indicators?.discovery_stale_days} />
            <div className="cards-grid" role="list" aria-label="Gold pairs">
              {!overview ? (
                Array.from({ length: 2 }).map((_, i) => <SkeletonCard key={i} />)
              ) : (
                Object.entries(PAIR_META).filter(([,m]) => m.bot === "gold").map(([pair]) => (
                <PairCard
                  key={pair}
                  pair={pair}
                  data={pairData[pair]}
                  strategy={strategyParams?.pairs?.[pair]?.strategy_type || "rsi_momentum"}
                  regime={liveRegimes[pair] || "—"}
                  livePrice={livePrices[pair]}
                  onSelect={handleSelectPair}
                  isSelected={selectedPair === pair}
                  botPaused={botStatus.gold === "paused"}
                />
              )))}
            </div>
          </section>

          <section className="bot-section">
            <BotToggle botName="crypto" label="Crypto" staleDays={overview?.bots?.crypto?.live_indicators?.discovery_stale_days} />
            <div className="cards-grid" role="list" aria-label="Crypto pairs">
              {!overview ? (
                Array.from({ length: 2 }).map((_, i) => <SkeletonCard key={i} />)
              ) : (
                Object.entries(PAIR_META).filter(([,m]) => m.bot === "crypto").map(([pair]) => (
                <PairCard
                  key={pair}
                  pair={pair}
                  data={pairData[pair]}
                  strategy={strategyParams?.pairs?.[pair]?.strategy_type || "rsi_momentum"}
                  regime={liveRegimes[pair] || "—"}
                  livePrice={livePrices[pair]}
                  onSelect={handleSelectPair}
                  isSelected={selectedPair === pair}
                  botPaused={botStatus.crypto === "paused"}
                />
              )))}
            </div>
            {overview && botHasPipelineGap("crypto", overview) && (
              <PipelineGap bot="crypto" />
            )}
          </section>

          {selectedPair && view === "live" && (
            <button className="scroll-top-btn" onClick={scrollToTop} title="Scroll to top">
              ↑
            </button>
          )}

          {view === "live" && selectedPair && (
            <section className="lower-grid-full" ref={detailRef}>
              <DetailPanel pair={selectedPair} botData={selectedBotData} strategyParams={strategyParams?.pairs} />
            </section>
          )}
        </>
      ) : view === "activity" ? (
        <div className="activity-page">
          <div className="activity-page-tabs">
            <button className={`ltab ${subTab === "activity" ? "ltab-active" : ""}`} onClick={() => setSubTab("activity")}>Activity</button>
            <button className={`ltab ${subTab === "skips" ? "ltab-active" : ""}`} onClick={() => setSubTab("skips")}>Skip Analysis</button>
            <button className={`ltab ${subTab === "versions" ? "ltab-active" : ""}`} onClick={() => setSubTab("versions")}>Versions</button>
          </div>
          <div className="activity-page-content">
            {subTab === "activity" ? (
              <ActivityFeed overview={overview} />
            ) : subTab === "versions" ? (
              <VersionView perVersion={perVersion} />
            ) : (
              <SkipAnalysis apiBase={API_BASE} botName={selectedBotName || "forex"} />
            )}
          </div>
        </div>
      ) : null}

      {/* Keep all tab panels mounted — hidden when inactive (instant switch, cached data) */}
      <div className={`tab-panel ${view === "reports" ? "tab-active" : "tab-hidden"}`}>
        <ReportsView apiBase={API_BASE} isActive={view === "reports"} />
      </div>
      <div className={`tab-panel ${view === "discovered" ? "tab-active" : "tab-hidden"}`}>
        <Suspense fallback={<SkeletonCard />}><DiscoveredView apiBase={API_BASE} isActive={view === "discovered"} /></Suspense>
      </div>
      <div className={`tab-panel ${view === "cortex" ? "tab-active" : "tab-hidden"}`}>
        <Suspense fallback={<SkeletonCard />}><CortexView apiBase={API_BASE} isActive={view === "cortex"} /></Suspense>
      </div>
      <div className={`tab-panel ${view === "audit" ? "tab-active" : "tab-hidden"}`}>
        <Suspense fallback={<SkeletonCard />}><AuditView apiBase={API_BASE} isActive={view === "audit"} /></Suspense>
      </div>
      <div className={`tab-panel ${view === "heartbeat" ? "tab-active" : "tab-hidden"}`}>
        <HeartbeatView apiBase={API_BASE} />
      </div>
      <div className={`tab-panel ${view === "flatline" ? "tab-active" : "tab-hidden"}`}>
        <FlatlineView apiBase={API_BASE} />
      </div>
      {view === "alerts" ? (
        <AlertsView alerts={alerts} dismiss={dismiss} />
      ) : null}

      <footer className="app-footer">
        Paper trading only · {view === "live" ? `refreshes every ${POLL_MS / 1000}s` : "auto-refreshing"} · click any{" "}
        <span className="gdot gdot-inline">?</span> to learn what a term means
      </footer>
      <ChatPopup />
    </div>
  );
}
