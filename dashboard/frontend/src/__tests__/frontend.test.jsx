// Phase 17 frontend tests (vitest + jsdom + @testing-library/react).
// Run: npm test   (or: pytest tests/test_frontend.py)

import React from "react";
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import App from "../App.jsx";

const AUTH_READY = { setup_required: false };
const AUTH_VALID = { valid: true };

function mockOverview() {
  return {
    ts: new Date().toISOString(),
    totals: { closed_trades: 2, open_trades: 0 },
    bots: {
      forex: {
        recent_trades: [{ pair: "EUR/USD", exit_reason: "tp", pnl_pct: 1.0, exit_ts: "2026-01-01T00:00:00Z" }],
        recent_skips: [],
        recent_hypotheses: [],
        recent_open_trades: [],
        closed_trades: 1,
        open_count: 0,
        heartbeat: { cycle: 1 },
        _received_at: "2026-01-01T00:00:00Z",
      },
      gold: {
        recent_trades: [{ pair: "XAU/USD", exit_reason: "sl", pnl_pct: -0.5, exit_ts: "2026-01-01T00:00:00Z" }],
        recent_skips: [],
        recent_hypotheses: [],
        recent_open_trades: [],
        closed_trades: 1,
        open_count: 0,
        heartbeat: { cycle: 1 },
        _received_at: "2026-01-01T00:00:00Z",
      },
      crypto: {
        recent_trades: [],
        recent_skips: [],
        recent_hypotheses: [],
        recent_open_trades: [],
        closed_trades: 0,
        open_count: 0,
        heartbeat: {},
      },
    },
    forex: { trades: 1 },
    gold: { trades: 1 },
    crypto: { trades: 0 },
  };
}

function mockDiscovered() {
  return {
    pairs: {
      "EUR/USD": [
        { name: "rsi_div", win_rate: 0.6, fitness: 0.8, _bot: "forex" },
        { name: "macd_cross", win_rate: 0.55, fitness: 0.7, _bot: "forex" },
      ],
    },
    ensemble: { "EUR/USD": { signal: 0.4 } },
    total_indicators: 2,
    total_pairs: 1,
    degradation: {},
    bots: {
      forex: { total_indicators: 2, total_pairs: 1 },
      gold: { total_indicators: 0, total_pairs: 0 },
      crypto: { total_indicators: 0, total_pairs: 0 },
    },
  };
}

function installFetchMock(overview = mockOverview()) {
  global.fetch = vi.fn(async (url) => {
    const u = String(url);
    if (u.includes("/api/auth/status")) {
      return { ok: true, status: 200, json: async () => AUTH_READY };
    }
    if (u.includes("/api/auth/verify")) {
      return { ok: true, status: 200, json: async () => AUTH_VALID };
    }
    if (u.includes("/api/overview")) {
      return { ok: true, status: 200, json: async () => overview };
    }
    if (u.includes("/api/discovered")) {
      return { ok: true, status: 200, json: async () => mockDiscovered() };
    }
    if (u.includes("/api/flatline/")) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (u.includes("/api/heartbeat/")) {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    if (u.includes("/api/alerts")) {
      return { ok: true, status: 200, json: async () => ({ alerts: [], count: 0 }) };
    }
    if (u.includes("/api/per-version/")) {
      return { ok: true, status: 200, json: async () => ({ versions: [] }) };
    }
    if (u.includes("/api/cortex")) {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    if (u.includes("/api/bot/") && u.includes("/pulse")) {
      return { ok: true, status: 200, json: async () => ({ desired_state: "running" }) };
    }
    if (u.includes("/api/strategy-params/")) {
      return { ok: true, status: 200, json: async () => ({ pairs: {} }) };
    }
    if (u.includes("/api/ui-config")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ bots: ["forex", "gold", "crypto", "btc"], title: "Hermes", scope: null }),
      };
    }
    if (u.includes("/api/spark")) {
      return { ok: true, status: 200, json: async () => ({ prices: [1.1, 1.11, 1.12] }) };
    }
    if (u.includes("/api/chart-analysis/")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          bot: "forex",
          chart_vision: true,
          cycle: 12,
          ts: Date.now() / 1000,
          market_closed: false,
          n_usable: 1,
          n_blocked: 1,
          pairs: [
            {
              pair: "EUR/USD",
              context: "trend: downtrend (conf=0.70). SR: 1.08. Rec: avoid entirely",
              price: 1.085,
              regime: "range",
              history: [1.08, 1.082, 1.085],
              usable: true,
              hard_block: true,
              soft_block: false,
              quality: 7,
              confidence: 0.7,
              trend: "downtrend",
              recommendation: "avoid entirely",
              sr_level: "1.08",
            },
          ],
        }),
      };
    }
    if (u.includes("/api/skip-analysis/")) {
      return { ok: true, status: 200, json: async () => ({ bot: "forex", by_pair: {}, total_skips: 0 }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
}

beforeEach(() => {
  localStorage.setItem("hermes_token", "test-token");
  localStorage.setItem("hermes_onboarded", "1");
  // Recharts ResponsiveContainer needs ResizeObserver (jsdom lacks it).
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  Element.prototype.scrollIntoView = vi.fn();
  installFetchMock();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("Phase 17 dashboard frontend", () => {
  it("test_overview_8_pairs", async () => {
    render(<App />);
    const cards = await screen.findAllByTestId("pair-card");
    expect(cards).toHaveLength(8);
  });

  it("test_bot_sections", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    expect(screen.getByLabelText("Forex pairs")).toBeInTheDocument();
    expect(screen.getByLabelText("Gold pairs")).toBeInTheDocument();
    expect(screen.getByLabelText("Crypto pairs")).toBeInTheDocument();
  });

  it("test_auto_refresh", async () => {
    vi.useFakeTimers();
    render(<App />);
    await act(async () => {
      await Promise.resolve();
    });
    const first = global.fetch.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(15_000);
    });
    expect(global.fetch.mock.calls.length).toBeGreaterThan(first);
  });

  it("test_discovered_tab", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    fireEvent.click(screen.getByRole("tab", { name: "Discovered" }));
    const items = await screen.findAllByTestId("gp-indicators");
    expect(items.length).toBeGreaterThan(0);
    expect(screen.getAllByText("rsi_div").length).toBeGreaterThan(0);
    expect(screen.getAllByText("macd_cross").length).toBeGreaterThan(0);
  });

  it("test_activity_charts_tab_before_skips", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    // ModeToggle label is current mode; click Watcher → Advanced.
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    fireEvent.click(screen.getByRole("tab", { name: "Activity" }));
    const charts = screen.getByRole("button", { name: "Charts" });
    const skips = screen.getByRole("button", { name: "Skip Analysis" });
    expect(charts.compareDocumentPosition(skips) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(charts);
    expect(await screen.findByTestId("chart-analysis")).toBeTruthy();
    expect(await screen.findByText("EUR/USD")).toBeTruthy();
    expect(screen.getByText(/L14 hard/i)).toBeTruthy();
    expect(screen.getByText(/chart_vision: true/i)).toBeTruthy();
  });

  it("test_discovered_crypto_filter_keeps_tabs", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    fireEvent.click(screen.getByRole("tab", { name: "Discovered" }));
    await screen.findByText("rsi_div");
    fireEvent.click(screen.getByRole("tab", { name: /crypto/i }));
    expect(await screen.findByTestId("discovered-empty-filter")).toBeTruthy();
    // Tabs must remain so the page does not look like a broken blank state.
    expect(screen.getByRole("tab", { name: "All" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /crypto/i })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    expect(await screen.findByText("rsi_div")).toBeTruthy();
  });

  it("test_discovered_gold_and_forex_empty_filters_keep_tabs", async () => {
    // Same blank-page bug path as crypto: filter to a bot with 0 rows.
    installFetchMock({
      ...mockOverview(),
    });
    global.fetch = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes("/api/auth/status")) {
        return { ok: true, status: 200, json: async () => AUTH_READY };
      }
      if (u.includes("/api/auth/verify")) {
        return { ok: true, status: 200, json: async () => AUTH_VALID };
      }
      if (u.includes("/api/overview")) {
        return { ok: true, status: 200, json: async () => mockOverview() };
      }
      if (u.includes("/api/discovered")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            pairs: {
              "BTC/USD": [
                { name: "btc_mom", win_rate: 0.5, fitness: 0.4, _bot: "crypto" },
              ],
            },
            ensemble: { "BTC/USD": { signal: 0.1 } },
            total_indicators: 1,
            total_pairs: 1,
            degradation: {},
            bots: {
              forex: { total_indicators: 0, total_pairs: 0 },
              gold: { total_indicators: 0, total_pairs: 0 },
              crypto: { total_indicators: 1, total_pairs: 1 },
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    fireEvent.click(screen.getByRole("tab", { name: "Discovered" }));
    await screen.findByText("btc_mom");

    for (const bot of ["gold", "forex"]) {
      fireEvent.click(screen.getByRole("tab", { name: new RegExp(bot, "i") }));
      expect(await screen.findByTestId("discovered-empty-filter")).toHaveTextContent(
        new RegExp(bot, "i"),
      );
      expect(screen.getByRole("tab", { name: "All" })).toBeTruthy();
      expect(screen.getByRole("tab", { name: /crypto/i })).toBeTruthy();
    }

    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    expect(await screen.findByText("btc_mom")).toBeTruthy();
  });

  it("test_empty_state_diagnostic", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    expect(await screen.findByTestId("pipeline-gap")).toHaveTextContent(
      "pipeline gap for crypto",
    );
  });

  it("test_btc_pullback_strategy_badge_not_rsi", async () => {
    const overview = mockOverview();
    overview.active_bots = ["forex", "gold", "crypto", "btc"];
    overview.bots.btc = {
      recent_trades: [],
      recent_skips: [],
      recent_hypotheses: [],
      recent_open_trades: [
        {
          id: "btc:BTC/USDT:1",
          pair: "BTC/USDT",
          entry_type: "pullback",
          entry_decision: "probe",
          size_mode: "probe",
          size_reason: "sentient_probe",
          probe_fraction: 0.5,
          chart_size_mult: 0.5,
          entry_price: 65000,
          size: 0.075,
          base_size: 0.15,
          entry_ts: "2026-01-01T00:00:00Z",
          held_cycles: 10,
          unrealised_pct: -0.1,
          peak_mfe_pct: 0,
          trough_mae_pct: -0.2,
          mfe_tracking: true,
        },
      ],
      closed_trades: 0,
      open_count: 1,
      heartbeat: { cycle: 1 },
      _received_at: "2026-01-01T00:00:00Z",
      strategy: {
        "BTC/USDT": { pair: "BTC/USDT", strategy_type: "donchian_breakout", version: "08" },
      },
    };
    overview.totals.open_trades = 1;
    installFetchMock(overview);
    const baseFetch = global.fetch;
    global.fetch = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes("/api/strategy-params/")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            pairs: { "BTC/USDT": { strategy_type: "donchian_breakout", version: "08" } },
          }),
        };
      }
      if (u.includes("/api/ui-config")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            bots: ["forex", "gold", "crypto", "btc"],
            title: "Hermes",
            scope: null,
          }),
        };
      }
      return baseFetch(url);
    });

    render(<App />);
    // Mode toggle shows current mode; click Watcher → Advanced (badges only on Advanced).
    fireEvent.click(await screen.findByRole("button", { name: /^Watcher$/i }));
    const badges = await screen.findAllByTestId("strategy-badge");
    const pullback = badges.find((el) => /Pullback/i.test(el.textContent || ""));
    expect(pullback).toBeTruthy();
    expect(pullback).not.toHaveTextContent(/RSI Momentum/i);
    const sizeBadge = await screen.findByTestId("size-mode-badge");
    expect(sizeBadge).toHaveTextContent(/Probe/i);
  });

  it("test_legacy_full_with_entry_decision_probe_shows_probe", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:legacy",
        pair: "EUR/USD",
        entry_type: "pullback",
        entry_decision: "probe",
        size_mode: "full", // legacy stamp — dashboard must still show Probe
        size: 0.075,
        base_size: 0.15,
        chart_size_mult: 0.5,
        entry_price: 1.1,
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 2,
        unrealised_pct: 0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("size-mode-badge");
    expect(badge).toHaveTextContent(/Probe 50%/i);
  });

  it("test_probe_size_mode_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:1",
        pair: "EUR/USD",
        entry_type: "mean_reversion",
        entry_price: 1.1,
        size: 0.1,
        base_size: 0.4,
        size_mode: "probe",
        evidence_n: 2,
        evidence_state: "thin",
        probe_fraction: 0.25,
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 3,
        unrealised_pct: 0.2,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    // Probe badge is Advanced-face only (same as strategy / GP badges).
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("size-mode-badge");
    expect(badge).toHaveTextContent(/Probe 25%/i);
  });

  it("test_expert_weight_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:2",
        pair: "EUR/USD",
        entry_type: "gp_ensemble",
        entry_price: 1.1,
        size: 0.1,
        size_mode: "full",
        expert_mode: "soft",
        expert_weight: 0.25,
        suppressed_soft: true,
        expert_reasons: ["soft_suppress"],
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 1,
        unrealised_pct: 0.0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("expert-weight-badge");
    expect(badge).toHaveTextContent(/W25%/i);
  });

  it("test_regime_mult_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:3",
        pair: "EUR/USD",
        entry_type: "mean_reversion",
        entry_price: 1.1,
        size: 0.16,
        regime_mode: "soft",
        regime_mult: 0.4,
        regime_label: "trend_down",
        fast_regime: "down",
        entry_regime: "trend",
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 1,
        unrealised_pct: 0.0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("regime-mult-badge");
    expect(badge).toHaveTextContent(/R40%/i);
  });

  it("test_kelly_mult_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:4",
        pair: "EUR/USD",
        entry_type: "mean_reversion",
        entry_price: 1.1,
        size: 0.12,
        kelly_mode: "soft",
        kelly_mult: 0.35,
        kelly_f: 0.08,
        p_bayes: 0.42,
        ci_low: 0.25,
        ci_high: 0.58,
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 1,
        unrealised_pct: 0.0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("kelly-mult-badge");
    expect(badge).toHaveTextContent(/K35%/i);
  });

  it("test_rank_score_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:5",
        pair: "EUR/USD",
        entry_type: "gp_ensemble",
        entry_price: 1.1,
        size: 0.2,
        ranking_mode: "soft",
        rank_score: 0.72,
        rank_reason: "best_score=0.72 > mean_reversion=0.61",
        rank_candidates: [
          { entry_type: "gp_ensemble", score: 0.72 },
          { entry_type: "mean_reversion", score: 0.61 },
        ],
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 1,
        unrealised_pct: 0.0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("rank-score-badge");
    expect(badge).toHaveTextContent(/Rank 0\.72/i);
  });

  it("test_book_mult_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:6",
        pair: "EUR/USD",
        entry_type: "mean_reversion",
        entry_price: 1.1,
        size: 0.15,
        book_mode: "soft",
        book_mult: 0.5,
        book_used: 0.5,
        book_cap: 1.0,
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 1,
        unrealised_pct: 0.0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("book-mult-badge");
    expect(badge).toHaveTextContent(/B50%/i);
  });

  it("test_exit_intel_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:7",
        pair: "EUR/USD",
        entry_type: "mean_reversion",
        entry_price: 1.1,
        size: 0.2,
        exit_intel_mode: "soft",
        be_trigger_frac: 0.65,
        trailing_atr_mult: 1.8,
        partial_enabled: true,
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 1,
        unrealised_pct: 0.0,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("exit-intel-badge");
    expect(badge).toHaveTextContent(/Exit/i);
  });

  it("test_mfe_peak_badge", async () => {
    const overview = mockOverview();
    overview.bots.forex.recent_open_trades = [
      {
        id: "forex:EUR/USD:8",
        pair: "EUR/USD",
        entry_type: "mean_reversion",
        entry_price: 1.1,
        size: 0.2,
        mfe_tracking: true,
        peak_mfe_pct: 1.25,
        trough_mae_pct: -0.4,
        entry_ts: "2026-01-01T00:00:00Z",
        held_cycles: 3,
        unrealised_pct: 0.8,
      },
    ];
    overview.bots.forex.open_count = 1;
    overview.totals.open_trades = 1;
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));
    const badge = await screen.findByTestId("mfe-peak-badge");
    expect(badge).toHaveTextContent(/MFE 1\.3%/i);
  });

  it("test_gp_ban_and_exclude_recommendation_in_detail", async () => {
    const overview = mockOverview();
    overview.bots.forex.gp_promote_gate = {
      "EUR/USD": {
        banned: false,
        seeded_from_env: false,
        env_listed: false,
        n: 40,
        expectancy: -0.08,
        last_reason: "hold_allowed",
        recommendation: "should_exclude",
        recommendation_reason: "expectancy -0.0800% <= ban threshold -0.05% with n=40",
      },
      "GBP/USD": {
        banned: true,
        seeded_from_env: false,
        env_listed: true,
        n: 40,
        expectancy: 0.12,
        last_reason: "hold_banned",
        recommendation: "should_include",
        recommendation_reason: "expectancy 0.1200% >= unban threshold 0.05% with n=40; still on GP_EXCLUDE_PAIRS; gate still banned",
      },
    };
    installFetchMock(overview);

    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByRole("button", { name: "Watcher" }));

    expect(await screen.findByTestId("gp-exclude-hint")).toHaveTextContent(/Exclude\?/i);
    expect(await screen.findByTestId("gp-banned-badge")).toHaveTextContent(/GP banned/i);
    expect(await screen.findByTestId("gp-include-hint")).toHaveTextContent(/Leave exclude/i);

    const eurCard = screen.getAllByTestId("pair-card").find((el) =>
      el.textContent?.includes("EUR/USD")
    );
    expect(eurCard).toBeTruthy();
    fireEvent.click(eurCard);
    const rec = await screen.findByTestId("detail-gp-recommendation");
    expect(rec).toHaveTextContent(/should be on GP exclude/i);
    expect(await screen.findByTestId("detail-gp-status")).toHaveTextContent(/Allowed/i);

    const gbpCard = screen.getAllByTestId("pair-card").find((el) =>
      el.textContent?.includes("GBP/USD")
    );
    fireEvent.click(gbpCard);
    const rec2 = await screen.findByTestId("detail-gp-recommendation");
    expect(rec2).toHaveTextContent(/should be out of GP exclude/i);
    expect(await screen.findByTestId("detail-gp-status")).toHaveTextContent(/Banned/i);
  });
});
