import React, { useMemo } from "react";
import { useApi } from "./_shared.jsx";
import { api } from "../api.js";
import { PAIRS_BY_BOT, DEFAULT_SCOPE } from "../bots.js";
import { PipelineGap, EmptyOr } from "./_shared.jsx";

// Overview: one card per pair in the active scope (default = forex+gold = 6 cards).
// Each card shows that bot's trade/hypothesis/skip counts from /api/overview,
// or the diagnostic pipeline gap if the bot never pushed.
export default function Overview({ bot, activeBots }) {
  const { data, error } = useApi(() => api.overview(), [bot]);

  const cards = useMemo(() => {
    const scope = bot === "all" ? DEFAULT_SCOPE : activeBots;
    return scope.flatMap((b) =>
      PAIRS_BY_BOT[b].map((pair) => ({ bot: b, pair })),
    );
  }, [bot, activeBots]);

  return (
    <section className="overview">
      <h2>Overview</h2>
      {error && <div className="error">overview error: {error}</div>}
      <div className="pair-cards" data-testid="pair-cards">
        {cards.map(({ bot: b, pair }) => {
          const counts = (data && data[b]) || null;
          return (
            <div
              key={`${b}:${pair}`}
              className="pair-card"
              data-testid="pair-card"
              data-bot={b}
              data-pair={pair}
            >
              <h3>{pair}</h3>
              <div className="bot-tag">{b}</div>
              <EmptyOr bot={b} data={counts}>
                {counts && (
                  <ul>
                    <li>trades: {counts.trades}</li>
                    <li>hypotheses: {counts.hypotheses}</li>
                    <li>skips: {counts.skips}</li>
                  </ul>
                )}
              </EmptyOr>
            </div>
          );
        })}
      </div>
    </section>
  );
}
