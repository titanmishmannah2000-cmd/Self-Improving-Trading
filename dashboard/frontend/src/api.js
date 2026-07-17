// Thin API client. The frontend ONLY talks to the Session-16 read API
// (never the database directly).

const BASE = "/api";

async function getJson(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    // 404 unknown bot / 401 etc. -> surface as null so views show "pipeline gap"
    return null;
  }
  return res.json();
}

export const api = {
  overview: () => getJson("/overview"),
  trades: (bot) => getJson(`/trades/${bot}`),
  hypotheses: (bot) => getJson(`/hypotheses/${bot}`),
  skips: (bot) => getJson(`/skips/${bot}`),
  discovered: (bot) => getJson(`/discovered/${bot}`),
  cortex: (bot) => getJson(`/cortex/${bot}`),
  flatline: (bot) => getJson(`/flatline/${bot}`),
  heartbeat: (bot) => getJson(`/heartbeat/${bot}`),
};
