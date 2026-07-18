import React, { useEffect, useState, useCallback } from "react";
import { api } from "../api.js";

// Diagnostic empty state (standing rule 4.4): never a silent blank panel.
// Shows exactly which bot has a pipeline gap so the operator verifies the
// data pipeline (bot push -> ingest -> SQL -> API) rather than blaming frontend.
export function PipelineGap({ bot }) {
  return (
    <div className="pipeline-gap" data-testid="pipeline-gap">
      pipeline gap for {bot}
    </div>
  );
}

export function EmptyOr({ bot, data, children, loading }) {
  if (loading) return <div className="loading">loading…</div>;
  if (data === null || data === undefined) return <PipelineGap bot={bot} />;
  if (Array.isArray(data) && data.length === 0) return <PipelineGap bot={bot} />;
  if (!Array.isArray(data) && typeof data === "object" && Object.keys(data).length === 0)
    return <PipelineGap bot={bot} />;
  return children;
}

const REFRESH_MS = 60_000;

// Shared hook: fetch on mount + every REFRESH_MS, expose data/error.
// `deps` re-fetches when the bot selector or tab changes.
export function useApi(fetcher, deps) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    fetcher()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    load();
    const id = setInterval(() => {
      load(); // direct re-fetch on the 60s tick
    }, REFRESH_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error };
}

// Generic per-tab fetcher used by Trades / Skips etc.
export function TabView({ endpoint, bot, activeBots, renderRow }) {
  const { data, error } = useApi(() => {
    if (bot !== "all") return apiFn(endpoint, bot);
    return Promise.all(activeBots.map((b) => apiFn(endpoint, b))).then((arrs) =>
      arrs.flat().map((r) => ({ ...r, _bot: r.bot || activeBots[0] })),
    );
  }, [bot, endpoint]);

  if (error) return <div className="error">{endpoint} error: {error}</div>;
  return (
    <section className={endpoint}>
      <h2>{endpoint}</h2>
      <EmptyOr bot={bot === "all" ? activeBots.join(",") : bot} data={data}>
        <table>
          <tbody>
            {data &&
              data.map((row, i) => (
                <tr key={i} data-bot={row._bot || row.bot}>
                  {renderRow(row)}
                </tr>
              ))}
          </tbody>
        </table>
      </EmptyOr>
    </section>
  );
}

function apiFn(endpoint, bot) {
  return api[endpoint](bot);
}
