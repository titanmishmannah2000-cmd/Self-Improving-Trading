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
    if (u.includes("/api/spark")) {
      return { ok: true, status: 200, json: async () => ({ prices: [1.1, 1.11, 1.12] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
}

beforeEach(() => {
  localStorage.setItem("hermes_token", "test-token");
  localStorage.setItem("hermes_onboarded", "1");
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
});
