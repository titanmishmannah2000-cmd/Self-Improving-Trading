import React, { useEffect, useState } from "react";
import { BOTS, DEFAULT_SCOPE, PAIRS_BY_BOT } from "./bots.js";
import { useApi } from "./views/_shared.jsx";
import Overview from "./views/Overview.jsx";
import Trades from "./views/Trades.jsx";
import Skips from "./views/Skips.jsx";
import Discovered from "./views/Discovered.jsx";
import Cortex from "./views/Cortex.jsx";
import Flatline from "./views/Flatline.jsx";
import Heartbeat from "./views/Heartbeat.jsx";
import LivePrices from "./views/LivePrices.jsx";

const TABS = [
  { id: "overview", label: "Overview", Comp: Overview },
  { id: "live", label: "Live Prices", Comp: LivePrices },
  { id: "trades", label: "Trades", Comp: Trades },
  { id: "skips", label: "Skips", Comp: Skips },
  { id: "discovered", label: "Discovered", Comp: Discovered },
  { id: "cortex", label: "Cortex", Comp: Cortex },
  { id: "flatline", label: "Flatline", Comp: Flatline },
  { id: "heartbeat", label: "Heartbeat", Comp: Heartbeat },
];

const REFRESH_MS = 60_000;

export default function App() {
  const [tab, setTab] = useState("overview");
  const [bot, setBot] = useState("all"); // "all" -> DEFAULT_SCOPE, else single bot

  const activeBots = bot === "all" ? DEFAULT_SCOPE : [bot];
  const Active = TABS.find((t) => t.id === tab).Comp;

  return (
    <div className="app">
      <header>
        <h1>HERMES Dashboard</h1>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.id}
              className={t.id === tab ? "tab active" : "tab"}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <label className="bot-selector">
          Bot:{" "}
          <select value={bot} onChange={(e) => setBot(e.target.value)}>
            <option value="all">all</option>
            {BOTS.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        </label>
      </header>

      <main>
        <Active bot={bot} activeBots={activeBots} />
      </main>
    </div>
  );
}
