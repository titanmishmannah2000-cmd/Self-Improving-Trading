import React from "react";
import { useApi } from "./_shared.jsx";
import { api } from "../api.js";
import { EmptyOr } from "./_shared.jsx";

export default function Flatline({ bot, activeBots }) {
  const { data, error } = useApi(() => {
    if (bot !== "all") return api.flatline(bot);
    return Promise.all(activeBots.map((b) => api.flatline(b))).then((arrs) =>
      arrs.flat(),
    );
  }, [bot]);

  if (error) return <div className="error">flatline error: {error}</div>;
  return (
    <section className="flatline">
      <h2>Flatline</h2>
      <EmptyOr bot={bot === "all" ? activeBots.join(",") : bot} data={data}>
        <ul className="flatline-log" data-testid="flatline-log">
          {data &&
            data.map((row, i) => (
              <li key={i}>{JSON.stringify(row)}</li>
            ))}
        </ul>
      </EmptyOr>
    </section>
  );
}
