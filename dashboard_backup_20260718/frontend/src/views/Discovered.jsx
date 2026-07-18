import React from "react";
import { useApi } from "./_shared.jsx";
import { api } from "../api.js";
import { EmptyOr } from "./_shared.jsx";

// Discovered: GP indicators listed per pair for the active bot(s).
export default function Discovered({ bot, activeBots }) {
  const { data, error } = useApi(() => {
    if (bot !== "all") return api.discovered(bot);
    return Promise.all(activeBots.map((b) => api.discovered(b))).then((arrs) =>
      arrs.flat().map((r) => ({ ...r, _bot: activeBots[0] })),
    );
  }, [bot]);

  if (error) return <div className="error">discovered error: {error}</div>;
  return (
    <section className="discovered">
      <h2>Discovered</h2>
      <EmptyOr bot={bot === "all" ? activeBots.join(",") : bot} data={data}>
        <ul className="gp-indicators" data-testid="gp-indicators">
          {data &&
            data.map((ind, i) => (
              <li key={i} data-bot={ind._bot} className="gp-indicator">
                {ind.name || ind.expression || JSON.stringify(ind)}
              </li>
            ))}
        </ul>
      </EmptyOr>
    </section>
  );
}
