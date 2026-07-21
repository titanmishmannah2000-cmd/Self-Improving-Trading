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
    bots: {
      forex: {
        recent_trades: [{ pair: "EUR/USD", exit_reason: "tp", pnl_pct: 1.0, exit_ts: "2026-01-01T00:00:00Z" }],
        recent_skips: [],
        recent_hypotheses: [],
        heartbeat: { cycle: 1 },
        _received_at: "2026-01-01T00:00:00Z",
      },
      gold: {
        recent_trades: [{ pair: "XAU/USD", exit_reason: "sl", pnl_pct: -0.5, exit_ts: "2026-01-01T00:00:00Z" }],
        recent_skips: [],
        recent_hypotheses: [],
        heartbeat: { cycle: 1 },
        _received_at: "2026-01-01T00:00:00Z",
      },
      crypto: {
        recent_trades: [],
        recent_skips: [],
        recent_hypotheses: [],
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
        { name: "rsi_div", win_rate: 0.6, fitness: 0.8 },
        { name: "macd_cross", win_rate: 0.55, fitness: 0.7 },
      ],
    },
    ensemble: { "EUR/USD": { signal: 0.4 } },
    total_indicators: 2,
    total_pairs: 1,
    degradation: {},
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
    fireEvent.click(screen.getByRole("tab", { name: "Discovered" }));
    const items = await screen.findAllByTestId("gp-indicators");
    expect(items.length).toBeGreaterThan(0);
    expect(screen.getAllByText("rsi_div").length).toBeGreaterThan(0);
    expect(screen.getAllByText("macd_cross").length).toBeGreaterThan(0);
  });

  it("test_empty_state_diagnostic", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    expect(await screen.findByTestId("pipeline-gap")).toHaveTextContent(
      "pipeline gap for crypto",
    );
  });
});
