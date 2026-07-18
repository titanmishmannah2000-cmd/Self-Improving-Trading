import React from "react";
import { useApi } from "./_shared.jsx";
import { api } from "../api.js";
import { EmptyOr } from "./_shared.jsx";

// Cortex: exiled indicators per active bot (from /api/cortex/{bot}).
export default function Cortex({ bot, activeBots }) {
  const { data, error } = useApi(() => {
    if (bot !== "all") return api.cortex(bot);
    return Promise.all(activeBots.map((b) => api.cortex(b))).then((arrs) => ({
      exiled: arrs.flatMap((a) => (a ? a.exiled : [])),
    }));
  }, [bot]);

  if (error) return <div className="error">cortex error: {error}</div>;
  return (
    <section className="cortex">
      <h2>Cortex</h2>
      <EmptyOr bot={bot === "all" ? activeBots.join(",") : bot} data={data}>
        <ul className="exiled" data-testid="exiled">
          {(data.exiled || []).map((name, i) => (
            <li key={i} className="exiled-item">
              {name}
            </li>
          ))}
        </ul>
      </EmptyOr>
    </section>
  );
}
