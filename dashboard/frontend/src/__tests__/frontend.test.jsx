// Phase 17 frontend tests (vitest + jsdom + @testing-library/react).
// Run: npm test   (or: pytest tests/test_frontend.py  -> invokes npm test)

import React from "react";
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import App from "../App.jsx";

// --- fetch mock: shape the S16 API would return --------------------------------
function mockOverview() {
  return {
    forex: { trades: 3, hypotheses: 1, skips: 0 },
    gold: { trades: 2, hypotheses: 0, skips: 1 },
    crypto: { trades: 0, hypotheses: 0, skips: 0 },
  };
}

beforeEach(() => {
  global.fetch = vi.fn(async (url) => {
    const body = url.includes("/overview")
      ? mockOverview()
      : url.includes("/discovered/")
        ? [{ name: "rsi_div", complexity: 3 }, { name: "macd_cross", complexity: 5 }]
        : [];
    return { ok: true, status: 200, json: async () => body };
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Phase 17 dashboard frontend", () => {
  it("test_overview_6_pairs", async () => {
    render(<App />);
    const cards = await screen.findAllByTestId("pair-card");
    expect(cards).toHaveLength(6);
  });

  it("test_bot_selector", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    const select = screen.getByRole("combobox");
    await act(async () => {
      fireEvent.change(select, { target: { value: "gold" } });
    });
    const cards = screen.getAllByTestId("pair-card");
    // gold owns exactly 2 pairs
    expect(cards).toHaveLength(2);
    for (const c of cards) {
      expect(c.getAttribute("data-bot")).toBe("gold");
    }
  });

  it("test_auto_refresh", async () => {
    vi.useFakeTimers();
    render(<App />);
    // flush the async mount fetches (React 18 StrictMode double-invokes effects)
    await act(async () => {
      await Promise.resolve();
    });
    const first = global.fetch.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    // at least one more fetch than the initial mount calls, without reload
    expect(global.fetch.mock.calls.length).toBeGreaterThan(first);
  });

  it("test_discovered_tab", async () => {
    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByText("Discovered"));
    const items = await screen.findAllByTestId("gp-indicators");
    expect(items.length).toBeGreaterThan(0);
    // default scope is forex+gold -> same indicator name may appear per bot
    expect(screen.getAllByText("rsi_div").length).toBeGreaterThan(0);
    expect(screen.getAllByText("macd_cross").length).toBeGreaterThan(0);
  });

  it("test_empty_state_diagnostic", async () => {
    // crypto has no pushes -> pipeline gap message, never blank
    global.fetch = vi.fn(async (url) => ({
      ok: true,
      status: 200,
      json: async () => (url.includes("/overview") ? mockOverview() : []),
    }));
    render(<App />);
    await screen.findAllByTestId("pair-card");
    fireEvent.click(screen.getByText("Trades"));
    // switch to crypto bot -> its trades endpoint returns [] -> pipeline gap
    const select = screen.getByRole("combobox");
    await act(async () => {
      fireEvent.change(select, { target: { value: "crypto" } });
    });
    expect(await screen.findByTestId("pipeline-gap")).toHaveTextContent(
      "pipeline gap for crypto",
    );
  });
});
