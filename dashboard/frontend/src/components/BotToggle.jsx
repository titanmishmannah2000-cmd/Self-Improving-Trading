import React, { useState, useCallback, useEffect } from "react";
import { API_BASE } from "../config.js";

export default function BotToggle({ botName, label, staleDays }) {
  const [state, setState] = useState("running");
  const [toggling, setToggling] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");

  const fetchState = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/bot/${botName}/pulse`);
      if (res.ok) {
        const json = await res.json();
        setState(json.desired_state);
      }
    } catch (e) { /* silent */ }
  }, [botName]);

  useEffect(() => { fetchState(); }, [fetchState]);

  const targetState = state === "running" ? "paused" : "running";
  const actionWord = targetState === "paused" ? "Pause" : "Resume";

  const openConfirm = () => {
    setConfirmText("");
    setConfirmOpen(true);
  };

  const closeConfirm = () => {
    setConfirmOpen(false);
    setConfirmText("");
  };

  const handleConfirm = async (e) => {
    if (e) e.preventDefault();
    if (confirmText !== actionWord) return;
    closeConfirm();
    setToggling(true);
    try {
      await fetch(`${API_BASE}/api/bot/${botName}/toggle`, { method: "POST" });
      await fetchState();
    } catch (e) { /* silent */ }
    setToggling(false);
  };

  const isOnline = state === "running";

  return (
    <>
      <div className={`bot-toggle ${isOnline ? "bt-on" : "bt-off"}`}>
      <div className="bt-info">
        <span className="bt-name">{label}</span>
        <span className={`bt-status ${isOnline ? "bt-status-on" : "bt-status-off"}`}>
          {isOnline ? "● Running" : "● Paused"}
        </span>
        {staleDays != null && (
          <span
            className={`bt-discovery ${staleDays >= 8 ? "bt-discovery-critical" : staleDays >= 7 ? "bt-discovery-warn" : "bt-discovery-ok"}`}
            title={`GP discovery ${staleDays}d ago`}
          >
            {staleDays >= 8 ? "🔴" : staleDays >= 7 ? "⚠️" : "✅"} GP: {staleDays}d
          </span>
        )}
      </div>
        <button
          className={`bt-btn ${toggling ? "bt-btn-loading" : ""}`}
          onClick={openConfirm}
          disabled={toggling}
          title={isOnline ? "Pause bot" : "Resume bot"}
        >
          {toggling ? "..." : isOnline ? "⏸ Pause" : "▶ Resume"}
        </button>
      </div>

      {confirmOpen && (
        <div className="confirm-overlay" onClick={closeConfirm}>
          <div className="confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3 className="confirm-title">
              {targetState === "paused" ? "⏸ Pause" : "▶ Resume"} {label}
            </h3>
            <p className="confirm-desc">
              Type <strong>{actionWord}</strong> to confirm.
            </p>
            <form onSubmit={handleConfirm}>
              <input
                type="text"
                className="confirm-input"
                placeholder={`Type "${actionWord}"`}
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                autoFocus
              />
              <div className="confirm-actions">
                <button type="button" className="confirm-btn confirm-btn-cancel" onClick={closeConfirm}>
                  Cancel
                </button>
                <button
                  type="submit"
                  className={`confirm-btn confirm-btn-go ${confirmText === actionWord ? "confirm-ready" : ""}`}
                  disabled={confirmText !== actionWord}
                >
                  {confirmText === actionWord ? `✓ ${actionWord}` : `${actionWord}`}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
