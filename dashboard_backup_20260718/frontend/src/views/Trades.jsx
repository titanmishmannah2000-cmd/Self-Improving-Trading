import React from "react";
import { TabView } from "./_shared.jsx";

export default function Trades({ bot, activeBots }) {
  return (
    <TabView
      endpoint="trades"
      bot={bot}
      activeBots={activeBots}
      renderRow={(r) => (
        <>
          <td>{r.pair}</td>
          <td>{r.pnl_pct}</td>
          <td>{r.strategy_type}</td>
        </>
      )}
    />
  );
}
