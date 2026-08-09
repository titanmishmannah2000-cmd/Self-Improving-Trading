import React, { useState, useEffect } from "react";
import { API_BASE } from "../config.js";

export function useAuth() {
  const [mode, setMode] = useState("loading");

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 12000);
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/auth/status`, { signal: ctrl.signal });
        const data = await res.json();
        if (data.setup_required) {
          setMode("setup");
          return;
        }
        const token = localStorage.getItem("hermes_token");
        if (!token) {
          setMode("login");
          return;
        }
        const vres = await fetch(`${API_BASE}/api/auth/verify`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: ctrl.signal,
        });
        const vdata = await vres.json();
        if (vdata.valid) {
          setMode("ready");
        } else {
          localStorage.removeItem("hermes_token");
          setMode("login");
        }
      } catch (e) {
        setMode("login");
      } finally {
        clearTimeout(timer);
      }
    })();
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, []);

  const setup = async (password, confirm) => {
    const res = await fetch(`${API_BASE}/api/auth/setup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, confirm }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(_authErr(data, "Setup failed"));
    localStorage.setItem("hermes_token", data.token);
    setMode("ready");
  };

  const login = async (password) => {
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(_authErr(data, "Login failed"));
    localStorage.setItem("hermes_token", data.token);
    setMode("ready");
  };

  const logout = () => {
    localStorage.removeItem("hermes_token");
    setMode("login");
  };

  return { mode, setup, login, logout };
}

function _authErr(data, fallback) {
  if (!data || typeof data !== "object") return fallback;
  const d = data.detail ?? data.error ?? data.message;
  if (typeof d === "string" && d.trim()) return d;
  if (Array.isArray(d) && d[0]?.msg) return String(d[0].msg);
  return fallback;
}

export function SetupScreen({ onSetup }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPw, setShowPw] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    if (password.length < 6) {
      setError("Password must be at least 6 characters");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setLoading(true);
    try {
      await onSetup(password, confirm);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1 className="auth-title">Hermes</h1>
        <p className="auth-subtitle">First time setup</p>
        <p className="auth-desc">Set a password to protect your private dashboard. You only do this once.</p>
        <form onSubmit={handleSubmit}>
          <div className="auth-field">
            <div className="auth-pw-wrap">
              <input
                type={showPw ? "text" : "password"}
                placeholder="New password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={6}
                className="auth-input"
                autoFocus
              />
              <button type="button" className="auth-pw-toggle" onClick={() => setShowPw((s) => !s)} tabIndex={-1}>
                {showPw ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          <div className="auth-field">
            <div className="auth-pw-wrap">
              <input
                type={showPw ? "text" : "password"}
                placeholder="Confirm password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                className="auth-input"
              />
            </div>
          </div>
          <p className="auth-hint">Minimum 6 characters</p>
          {error && <p className="auth-error">{error}</p>}
          <button type="submit" className="auth-btn" disabled={loading}>
            {loading ? "Setting up..." : "Set Password"}
          </button>
        </form>
      </div>
    </div>
  );
}

export function LoginScreen({ onLogin }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPw, setShowPw] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await onLogin(password);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1 className="auth-title">Hermes</h1>
        <p className="auth-subtitle">Private dashboard</p>
        <form onSubmit={handleSubmit}>
          <div className="auth-field">
            <div className="auth-pw-wrap">
              <input
                type={showPw ? "text" : "password"}
                placeholder="Password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="auth-input"
                autoFocus
              />
              <button type="button" className="auth-pw-toggle" onClick={() => setShowPw((s) => !s)} tabIndex={-1}>
                {showPw ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          {error && <p className="auth-error">{error}</p>}
          <button type="submit" className="auth-btn" disabled={loading}>
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
