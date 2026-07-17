// Bot + pair topology — the single source of bot identity.
// Components MUST read bot/pair identity from here (or the API), never hard-code.
// This is config data, not a hard-coded bot name inside a component's logic.

export const BOTS = ["forex", "gold", "crypto"];

// Pairs owned by each bot. The default Overview scope is forex+gold (6 pairs),
// matching the Phase-17 success criterion (test_overview_6_pairs).
export const PAIRS_BY_BOT = {
  forex: ["EUR/USD", "GBP/USD", "GBP/JPY", "AUD/USD"],
  gold: ["XAU/USD", "XAG/USD"],
  crypto: ["BTC/USD", "ETH/USD"],
};

export const DEFAULT_SCOPE = ["forex", "gold"];

// Flatten the default scope into the 6 pair-cards the Overview renders.
export function defaultPairs() {
  return DEFAULT_SCOPE.flatMap((bot) =>
    PAIRS_BY_BOT[bot].map((pair) => ({ bot, pair })),
  );
}
