"""findings_store.py — SQLite persistence for Hermes Self-Audit System.

Three tables:
  - audit_findings: individual findings from each audit run
  - audit_runs: metadata about each audit run batch
  - audit_maturity: maturity scores per domain per run (tracks improvement over time)
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DB_DIR = Path(__file__).parent / "audit_state"
DB_PATH = DB_DIR / "audit.db"


def _get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call every startup/run."""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_findings (
                id              TEXT PRIMARY KEY,
                domain          TEXT NOT NULL,
                type            TEXT NOT NULL,  -- BUG | GAP | RISK | UPGRADE | INTELLIGENCE
                severity        TEXT NOT NULL,  -- CRITICAL | HIGH | MEDIUM | LOW | N/A
                location        TEXT,
                description     TEXT NOT NULL,
                trading_impact  TEXT,
                suggested_fix   TEXT,
                confidence      INTEGER DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | applied
                progress        INTEGER DEFAULT 0,              -- 0-100: 0=not started, 100=complete
                assignee        TEXT DEFAULT '',                -- who's working on it
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                audit_run_id    TEXT,
                prior_finding_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_findings_status ON audit_findings(status);
            CREATE INDEX IF NOT EXISTS idx_findings_domain ON audit_findings(domain);
            CREATE INDEX IF NOT EXISTS idx_findings_run ON audit_findings(audit_run_id);
            CREATE INDEX IF NOT EXISTS idx_findings_severity ON audit_findings(severity);
        """)
        # Migration: add progress column if missing (older DBs)
        try:
            conn.execute("ALTER TABLE audit_findings ADD COLUMN progress INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE audit_findings ADD COLUMN assignee TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_runs (
                id              TEXT PRIMARY KEY,
                domains_run     TEXT NOT NULL,         -- JSON array
                findings_count  INTEGER DEFAULT 0,
                critical_count  INTEGER DEFAULT 0,
                resolved_prior  TEXT DEFAULT '[]',     -- JSON array of prior finding IDs
                regressions     TEXT DEFAULT '[]',     -- JSON array of regressed finding IDs
                maturity_scores TEXT DEFAULT '{}',     -- JSON object: {domain: score}
                intelligence_maturity TEXT DEFAULT '{}', -- JSON object: {domain: intel_score}
                summary_json    TEXT,                   -- Full LLM response JSON for re-inspection
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_maturity (
                id             TEXT PRIMARY KEY,
                domain         TEXT NOT NULL,
                score          INTEGER NOT NULL,       -- 1-5 correctness maturity
                intelligence_score INTEGER DEFAULT 0,   -- 0-5 intelligence maturity (0 = not evaluated)
                justification  TEXT,
                compared_to_prior TEXT DEFAULT 'first_audit',  -- improved | same | regressed | first_audit
                audit_run_id   TEXT,
                created_at     TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_maturity_domain ON audit_maturity(domain);
        """)
        
        # Migration: add audit_progress table if missing
        try:
            conn.execute("SELECT 1 FROM audit_progress LIMIT 1")
        except sqlite3.OperationalError:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_progress (
                    id              TEXT PRIMARY KEY,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    progress_pct    INTEGER DEFAULT 0,
                    current_domain  TEXT DEFAULT '',
                    domains_total   INTEGER DEFAULT 5,
                    domains_done    INTEGER DEFAULT 0,
                    message         TEXT DEFAULT '',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
            """)
        conn.commit()
    finally:
        conn.close()


# ── Findings CRUD ──────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def insert_finding(
    domain: str,
    finding_type: str,
    severity: str,
    location: Optional[str],
    description: str,
    trading_impact: Optional[str],
    suggested_fix: Optional[str],
    confidence: int,
    audit_run_id: str,
    prior_finding_id: Optional[str] = None,
) -> dict:
    """Insert one finding. Returns the full row as dict."""
    f_id = _new_id()
    now = _ts()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO audit_findings
               (id, domain, type, severity, location, description,
                trading_impact, suggested_fix, confidence, status,
                created_at, updated_at, audit_run_id, prior_finding_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (f_id, domain, finding_type, severity, location, description,
             trading_impact, suggested_fix, confidence,
             now, now, audit_run_id, prior_finding_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_finding(f_id)


def insert_findings_batch(findings: list[dict], audit_run_id: str) -> int:
    """Batch insert multiple findings. Returns count inserted."""
    now = _ts()
    conn = _get_conn()
    count = 0
    try:
        rows = []
        for f in findings:
            f_id = _new_id()
            rows.append((
                f_id,
                f.get("domain", ""),
                f.get("type", f.get("finding_type", "")),
                f.get("severity", "N/A"),
                f.get("location"),
                f.get("description", ""),
                f.get("trading_impact"),
                f.get("suggested_fix"),
                f.get("confidence", 0),
                now,
                now,
                audit_run_id,
                f.get("prior_finding_id"),
            ))
        conn.executemany(
            """INSERT INTO audit_findings
               (id, domain, type, severity, location, description,
                trading_impact, suggested_fix, confidence, status,
                created_at, updated_at, audit_run_id, prior_finding_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        count = len(rows)
    finally:
        conn.close()
    return count


# ── Audit Progress ──

def create_audit_progress(audit_run_id: str, domains_total: int = 5) -> dict:
    """Create a new audit progress entry. Returns the row as dict."""
    now = _ts()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_progress (id, status, progress_pct, domains_total, created_at, updated_at) VALUES (?, 'pending', 0, ?, ?, ?)",
            (audit_run_id, domains_total, now, now),
        )
        conn.commit()
        return {"id": audit_run_id, "status": "pending", "progress_pct": 0,
                "current_domain": "", "domains_total": domains_total, "domains_done": 0,
                "message": "Queued", "created_at": now, "updated_at": now}
    finally:
        conn.close()


def update_audit_progress(audit_run_id: str, **kwargs) -> dict:
    """Update audit progress. Kwargs: status, progress_pct, current_domain, domains_done, message."""
    ALLOWED = {"status", "progress_pct", "current_domain", "domains_done", "message"}
    filtered = {k: v for k, v in kwargs.items() if k in ALLOWED}
    if not filtered:
        return {"error": "No valid progress fields"}
    now = _ts()
    sets = ", ".join(f"{k}=?" for k in filtered)
    vals = list(filtered.values()) + [now, audit_run_id]
    conn = _get_conn()
    try:
        conn.execute(f"UPDATE audit_progress SET {sets}, updated_at=? WHERE id=?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM audit_progress WHERE id=?", (audit_run_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_audit_progress(audit_run_id: str) -> Optional[dict]:
    """Get current audit progress."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM audit_progress WHERE id=?", (audit_run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_audit_progress() -> Optional[dict]:
    """Get the most recent audit progress entry."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM audit_progress ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_finding(finding_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM audit_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_findings(
    status: Optional[str] = None,
    domain: Optional[str] = None,
    severity: Optional[str] = None,
    finding_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List findings with optional filters. Ordered by created_at DESC."""
    conn = _get_conn()
    try:
        where_clauses = []
        params = []
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if domain:
            where_clauses.append("domain = ?")
            params.append(domain)
        if severity:
            where_clauses.append("severity = ?")
            params.append(severity)
        if finding_type:
            where_clauses.append("type = ?")
            params.append(finding_type)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        rows = conn.execute(
            f"SELECT * FROM audit_findings WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_finding_status(finding_id: str, new_status: str) -> Optional[dict]:
    """Update status to approved | rejected | applied | in_progress. Returns updated row."""
    valid = {"approved", "rejected", "applied", "in_progress"}
    if new_status not in valid:
        raise ValueError(f"Status must be one of: {valid}")
    now = _ts()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE audit_findings SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, finding_id),
        )
        # Auto-set progress when status changes meaningfully
        if new_status == "applied":
            conn.execute(
                "UPDATE audit_findings SET progress = 100 WHERE id = ? AND progress < 100",
                (finding_id,),
            )
        elif new_status == "approved":
            conn.execute(
                "UPDATE audit_findings SET progress = 0 WHERE id = ? AND progress IS NULL",
                (finding_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return get_finding(finding_id)


def update_finding_progress(finding_id: str, progress: int, assignee: str = "") -> Optional[dict]:
    """Update progress (0-100) and optional assignee. Returns updated row."""
    progress = max(0, min(100, progress))
    now = _ts()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE audit_findings SET progress = ?, assignee = ?, updated_at = ? WHERE id = ?",
            (progress, assignee, now, finding_id),
        )
        # Auto-set status to applied if progress reaches 100
        if progress >= 100:
            conn.execute(
                "UPDATE audit_findings SET status = 'applied', updated_at = ? WHERE id = ? AND status IN ('approved', 'in_progress', 'pending')",
                (now, finding_id),
            )
        elif progress > 0:
            # Set status to in_progress if it was approved
            conn.execute(
                "UPDATE audit_findings SET status = 'in_progress', updated_at = ? WHERE id = ? AND status = 'approved'",
                (now, finding_id),
            )
        conn.commit()
    finally:
        conn.close()
    return get_finding(finding_id)


# ── Audit Runs ─────────────────────────────────────────────────────────────

def create_run(domains: list[str]) -> dict:
    """Create a new audit run record. Returns the run dict."""
    run_id = _new_id()
    now = _ts()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO audit_runs
               (id, domains_run, findings_count, critical_count, created_at)
               VALUES (?, ?, 0, 0, ?)""",
            (run_id, json.dumps(domains), now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run_id)


def finish_run(
    run_id: str,
    findings_count: int,
    critical_count: int,
    resolved_prior: list[str],
    regressions: list[str],
    maturity_scores: dict,
    intelligence_maturity: dict,
    summary_json: Optional[str] = None,
):
    """Update a run with final counts and metadata."""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE audit_runs SET
               findings_count = ?, critical_count = ?,
               resolved_prior = ?, regressions = ?,
               maturity_scores = ?, intelligence_maturity = ?,
               summary_json = ?
               WHERE id = ?""",
            (findings_count, critical_count,
             json.dumps(resolved_prior), json.dumps(regressions),
             json.dumps(maturity_scores), json.dumps(intelligence_maturity),
             summary_json, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_run(run_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM audit_runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_runs(limit: int = 10) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest_run() -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM audit_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── Maturity Tracking ─────────────────────────────────────────────────────

def insert_maturity(
    domain: str,
    score: int,
    intelligence_score: int,
    justification: str,
    compared_to_prior: str,
    audit_run_id: str,
) -> dict:
    m_id = _new_id()
    now = _ts()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO audit_maturity
               (id, domain, score, intelligence_score, justification,
                compared_to_prior, audit_run_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (m_id, domain, score, intelligence_score, justification,
             compared_to_prior, audit_run_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM audit_maturity WHERE id = ?", (m_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_maturity_history(domain: str, limit: int = 20) -> list[dict]:
    """Return maturity scores for a domain, newest first."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_maturity WHERE domain = ? ORDER BY created_at DESC LIMIT ?",
            (domain, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest_maturity_per_domain() -> dict:
    """Return {domain: {score, intelligence_score, justification, compared_to_prior}} for latest run."""
    latest_run = get_latest_run()
    if not latest_run:
        return {}
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_maturity WHERE audit_run_id = ?",
            (latest_run["id"],),
        ).fetchall()
        result = {}
        for r in rows:
            result[r["domain"]] = {
                "score": r["score"],
                "intelligence_score": r["intelligence_score"],
                "justification": r["justification"],
                "compared_to_prior": r["compared_to_prior"],
            }
        return result
    finally:
        conn.close()


# ── Summary Stats ──────────────────────────────────────────────────────────

def get_summary_stats() -> dict:
    """Return overview stats for the dashboard summary endpoint."""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) as c FROM audit_findings").fetchone()["c"]
        open_count = conn.execute(
            "SELECT COUNT(*) as c FROM audit_findings WHERE status IN ('pending', 'approved')"
        ).fetchone()["c"]
        resolved = conn.execute(
            "SELECT COUNT(*) as c FROM audit_findings WHERE status = 'applied'"
        ).fetchone()["c"]
        rejected = conn.execute(
            "SELECT COUNT(*) as c FROM audit_findings WHERE status = 'rejected'"
        ).fetchone()["c"]
        critical_open = conn.execute(
            "SELECT COUNT(*) as c FROM audit_findings WHERE severity = 'CRITICAL' AND status IN ('pending', 'approved')"
        ).fetchone()["c"]
        latest_run = get_latest_run()

        # Per-domain breakdown
        by_domain = {}
        for r in conn.execute(
            "SELECT domain, COUNT(*) as c FROM audit_findings GROUP BY domain"
        ).fetchall():
            by_domain[r["domain"]] = r["c"]

        # Maturity for latest run
        latest_maturity = get_latest_maturity_per_domain()

        return {
            "total_findings": total,
            "open_findings": open_count,
            "applied_findings": resolved,
            "rejected_findings": rejected,
            "critical_open": critical_open,
            "by_domain": by_domain,
            "latest_maturity": latest_maturity,
            "latest_run_id": latest_run["id"] if latest_run else None,
            "latest_run_at": latest_run["created_at"] if latest_run else None,
        }
    finally:
        conn.close()


# ── Bootstrap ──────────────────────────────────────────────────────────────

# Auto-init on import
init_db()
