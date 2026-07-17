import React, { useEffect, useRef, useState } from "react";
import { PAIRS_BY_BOT } from "../bots.js";

// Live price board. Subscribes to the dashboard's Server-Sent Events stream
// (GET /api/price/{bot}/stream) so prices update the instant the bot sees
// them — no 60s poll. Falls back to the last snapshot on disconnect.
export default function LivePrices({ bot, activeBots }) {
  const bots = bot === "all" ? activeBots : [bot];
  const [prices, setPrices] = useState({}); // { "crypto:BTC/USD": {price, ts} }
  const esRef = useRef(null);

  useEffect(() => {
    // Reset cache when the bot selector changes.
    setPrices({});
    let closed = false;
    const sources = bots.map((b) => {
      const url = `/api/price/${b}/stream`;
      const es = new EventSource(url);
      es.onmessage = (ev) => {
        if (closed) return;
        try {
          const msg = JSON.parse(ev.data);
          const botName = msg.bot;
          const incoming = msg.prices || {};
          setPrices((prev) => {
            const next = { ...prev };
            for (const [pair, price] of Object.entries(incoming)) {
              next[`${botName}:${pair}`] = { price, ts: msg.ts };
            }
            return next;
          });
        } catch {
          /* ignore malformed event */
        }
      };
      return es;
    });
    esRef.current = sources;
    return () => {
      closed = true;
      sources.forEach((es) => es.close());
    };
  }, [bot, activeBots.join(",")]);

  // Flatten selected bots' pairs into rows.
  const rows = [];
  for (const b of bots) {
    for (const pair of PAIRS_BY_BOT[b] || []) {
      const entry = prices[`${b}:${pair}`];
      rows.push({ bot: b, pair, price: entry ? entry.price : null, ts: entry ? entry.ts : null });
    }
  }

  return (
    <section className="live-prices">
      <h2>Live Prices</h2>
      <p className="hint">Real-time via server-sent events (updates on every tick).</p>
      <table>
        <thead>
          <tr>
            <th>bot</th>
            <th>pair</th>
            <th>price</th>
            <th>updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.bot}:${r.pair}`} data-testid="live-price-row">
              <td>{r.bot}</td>
              <td>{r.pair}</td>
              <td className="price">
                {r.price === null ? <span className="pipeline-gap">no data yet</span> : r.price}
              </td>
              <td>{r.ts ? new Date(r.ts).toLocaleTimeString() : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
