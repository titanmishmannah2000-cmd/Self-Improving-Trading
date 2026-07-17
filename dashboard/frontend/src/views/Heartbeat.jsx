import React from "react";
import { useApi } from "./_shared.jsx";
import { api } from "../api.js";
import { EmptyOr } from "./_shared.jsx";

export default function Heartbeat({ bot, activeBots }) {
  const { data, error } = useApi(() => {
    if (bot !== "all") return api.heartbeat(bot);
    return Promise.all(activeBots.map((b) => api.heartbeat(b))).then((arrs) => ({
      heartbeats: arrs.filter(Boolean),
    }));
  }, [bot]);

  if (error) return <div className="error">heartbeat error: {error}</div>;
  return (
    <section className="heartbeat">
      <h2>Heartbeat</h2>
      <EmptyOr bot={bot === "all" ? activeBots.join(",") : bot} data={data}>
        <ul className="heartbeat-log" data-testid="heartbeat-log">
          {(data.heartbeats || []).map((hb, i) => (
            <li key={i}>{JSON.stringify(hb)}</li>
          ))}
        </ul>
      </EmptyOr>
    </section>
  );
}
