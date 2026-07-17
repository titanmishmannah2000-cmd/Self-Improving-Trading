import React from "react";
import { TabView } from "./_shared.jsx";

export default function Skips({ bot, activeBots }) {
  return (
    <TabView
      endpoint="skips"
      bot={bot}
      activeBots={activeBots}
      renderRow={(r) => (
        <>
          <td>{r.pair}</td>
          <td>{r.reason_skipped}</td>
        </>
      )}
    />
  );
}
