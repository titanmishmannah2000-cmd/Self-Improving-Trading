/**
 * Resolve the dashboard API base URL at runtime.
 * Vite env vars are build-time only; on Railway the web service knows the API
 * hostname via RAILWAY_SERVICE_* vars, and production hostnames follow a
 * predictable web → api substitution.
 */
export function resolveApiBase() {
  if (import.meta.env.VITE_API_BASE) {
    return import.meta.env.VITE_API_BASE.replace(/\/$/, "");
  }

  if (typeof window !== "undefined") {
    const { hostname, protocol } = window.location;
    if (hostname === "localhost" || hostname === "127.0.0.1") {
      return "http://localhost:8000";
    }

    // In Railway production, the web service proxies /api/* to the API service.
    // Use the same-origin path so the current frontend always talks to the latest API.
    if (hostname.includes("railway.app") || hostname.includes("hermes-dashboard-web")) {
      return "";
    }
  }

  return "http://localhost:8000";
}

export const API_BASE = resolveApiBase();

// Keep production requests on the current frontend/API contract.
// This avoids stale host rewrites and ensures future redeploys use the latest API path.
export const API_BASE_EFFECTIVE = API_BASE || "/";
