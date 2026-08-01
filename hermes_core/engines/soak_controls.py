"""Soak-readiness controls: halt, price sanity, state bootstrap, feed SLOs, DD halt.

Used by the live loop and self-audit. Fail-soft helpers never raise into the
trade cycle.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from hermes_core.env import get_env
from hermes_core.state.atomic_json import atomic_write_json, load_json
from hermes_core.state.paths import bot_state_dir

# FX stub ladder observed in polluted local heartbeats.
_FX_STUB_SET = frozenset({1.1, 1.11, 1.12, 1.13})
_FX_PAIRS = frozenset({"EUR/USD", "GBP/USD", "AUD/USD", "GBP/JPY"})
# chart_error is fail-open in the loop (cycle continues) — do not trip feed SLO.
# Halt-echo skips (feed_slo:/idle_slo:) are consequences of a halt, not feed
# measurements — counting them would deadlock recovery.
_FEED_SKIP_PREFIXES = ("fetch_error", "no_candle")
_FEED_SKIP_ECHO_PREFIXES = ("feed_slo:", "idle_slo:", "halt:feed", "halt:idle")


def _is_feed_failure_reason(reason: str) -> bool:
    r = str(reason or "")
    if any(r.startswith(p) for p in _FEED_SKIP_ECHO_PREFIXES):
        return False
    return r.startswith(_FEED_SKIP_PREFIXES)

_STATE_TOUCH_FILES = (
    "trades.jsonl",
    "skips.jsonl",
    "gp_shadow.jsonl",
    "flatline_log.jsonl",
    "hypotheses.jsonl",
)
# Halt reasons that may auto-clear when the underlying SLO recovers.
_RECOVERABLE_HALT_PREFIXES = ("idle_slo:", "feed_slo:", "halt:idle_slo", "halt:feed_slo")
# JSONL files that grow every cycle — rotate when past this many lines.
_JSONL_ROTATE_MAX = {
    "skips.jsonl": 20_000,
    "gp_shadow.jsonl": 10_000,
}
_JSONL_ROTATE_KEEP = {
    "skips.jsonl": 5_000,
    "gp_shadow.jsonl": 3_000,
}
# Flat PnL absolute threshold (percent) — treat as neutral, not a loss.
FLAT_PNL_EPS = 1e-6
# Consecutive mark failures before forcing a data-halt exit on an open.
DATA_HALT_EXIT_AFTER = 5


def halt_path(bot: str) -> Path:
    return bot_state_dir(bot) / "halt"


def open_positions_path(bot: str) -> Path:
    return bot_state_dir(bot) / "open_positions.json"


def cycle_state_path(bot: str) -> Path:
    return bot_state_dir(bot) / "cycle_state.json"


def bb_samples_path(bot: str) -> Path:
    return bot_state_dir(bot) / "bb_bw_samples.jsonl"


def read_halt(bot: str) -> dict[str, Any] | None:
    """Return halt payload or None."""
    p = halt_path(bot)
    if not p.exists():
        return None
    data = load_json(p, default=None, quarantine=False)
    if isinstance(data, dict):
        return data
    # Legacy one-line / jsonl halt files.
    try:
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            return {"ts": 0.0, "reason": "halt:file"}
        data = json.loads(raw.splitlines()[0])
        return data if isinstance(data, dict) else {"ts": 0.0, "reason": "halt:file"}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"ts": 0.0, "reason": "halt:file"}


def entries_halted(bot: str) -> tuple[bool, str]:
    """True when env HALT_ENTRIES=1 or ``{bot}/state/halt`` exists."""
    if get_env("HALT_ENTRIES", "0").strip() in ("1", "true", "TRUE", "yes", "YES"):
        return True, "halt:env"
    p = halt_path(bot)
    if p.exists():
        payload = read_halt(bot) or {}
        reason = str(payload.get("reason") or "halt:file")
        return True, reason
    return False, ""


def write_halt(bot: str, reason: str, *, alert: bool = True) -> bool:
    """Create halt file so new entries stop (exits still run). Returns success."""
    p = halt_path(bot)
    payload = {"ts": time.time(), "reason": reason}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(p, payload)
    except OSError as exc:
        if alert:
            with contextlib.suppress(Exception):
                from hermes_core.notify import send_text_alert

                send_text_alert(
                    f"[halt] {bot}: FAILED to write halt ({reason}): {exc!r}",
                    bot=bot,
                    pair="*",
                    guard="halt_write_fail",
                )
        return False
    if alert:
        with contextlib.suppress(Exception):
            from hermes_core.notify import send_text_alert

            send_text_alert(
                f"[halt] {bot}: entries halted — {reason}",
                bot=bot,
                pair="*",
                guard="halt_on",
            )
    return True


def clear_halt(bot: str, *, alert: bool = False, reason: str = "cleared") -> bool:
    existed = halt_path(bot).exists()
    with contextlib.suppress(OSError):
        halt_path(bot).unlink(missing_ok=True)
    if existed and alert:
        with contextlib.suppress(Exception):
            from hermes_core.notify import send_text_alert

            send_text_alert(
                f"[halt] {bot}: cleared — {reason}",
                bot=bot,
                pair="*",
                guard="halt_off",
            )
    return True


def maybe_recover_halt(
    bot: str,
    *,
    feed: dict[str, Any] | None = None,
    idle: dict[str, Any] | None = None,
    price_ok: bool = True,
) -> dict[str, Any]:
    """Auto-clear recoverable halt (idle/feed) when SLOs and prices are healthy.

    Price-sanity / DD / env / operator halts are NOT auto-cleared.
    """
    halted, reason = entries_halted(bot)
    if not halted:
        return {"recovered": False, "reason": reason, "halted": False}
    if reason == "halt:env" or get_env("HALT_ENTRIES", "0").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    ):
        return {"recovered": False, "reason": reason, "halted": True}
    recoverable = any(reason.startswith(p) for p in _RECOVERABLE_HALT_PREFIXES)
    if not recoverable:
        # Also treat bare halt:file with payload reason inside read_halt.
        payload = read_halt(bot) or {}
        inner = str(payload.get("reason") or "")
        recoverable = any(inner.startswith(p) for p in _RECOVERABLE_HALT_PREFIXES)
        if recoverable:
            reason = inner
    if not recoverable:
        return {"recovered": False, "reason": reason, "halted": True}

    feed = feed or feed_error_rate(bot_state_dir(bot) / "skips.jsonl")
    idle_hours = {"crypto": 4.0, "btc": 4.0, "gold": 8.0, "forex": 6.0}.get(bot, 6.0)
    idle = idle or idle_skip_slo(bot_state_dir(bot) / "skips.jsonl", hours=idle_hours)
    feed_ok = bool(feed.get("ok", True))
    # Sticky feed_slo trap: halt-echo skips + old no_candle keep the long window
    # red forever. Re-check a fresh window (echoes ignored) before giving up.
    if (not feed_ok) and str(reason).startswith("feed_slo:") and price_ok:
        fresh = feed_error_rate(
            bot_state_dir(bot) / "skips.jsonl", window=80, max_age_s=15 * 60.0
        )
        if fresh.get("ok", True) or int(fresh.get("feed_n") or 0) == 0:
            feed_ok = True
    idle_ok = not bool(idle.get("effectively_paused"))
    if feed_ok and idle_ok and price_ok:
        clear_halt(bot, alert=True, reason=f"recovered_from:{reason}")
        return {"recovered": True, "reason": reason, "halted": False}
    return {"recovered": False, "reason": reason, "halted": True}


def ensure_state_files(bot: str) -> Path:
    """Ensure canonical runtime files exist under ``{bot}/state/``."""
    d = bot_state_dir(bot)
    for name in _STATE_TOUCH_FILES:
        p = d / name
        if not p.exists():
            with contextlib.suppress(OSError):
                p.touch()
    (d / "discovered").mkdir(parents=True, exist_ok=True)
    (d / "cortex").mkdir(parents=True, exist_ok=True)
    (d / "strategies").mkdir(parents=True, exist_ok=True)
    (d / "archive").mkdir(parents=True, exist_ok=True)
    return d


def reset_reflection_latches(bot: str) -> None:
    """Wipe reflection latches after a trade scrub so counts match the book."""
    p = bot_state_dir(bot) / ".reflection_latches.json"
    try:
        if p.exists():
            atomic_write_json(p, {})
    except OSError:
        pass


def _round_price(p: float) -> float:
    return round(float(p), 6)


def price_sanity_pair(pair: str, price: float | None) -> tuple[bool, str]:
    """Reject placeholder / impossible single quotes."""
    if price is None:
        return False, "price_sanity:none"
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False, "price_sanity:nan"
    if px <= 0:
        return False, "price_sanity:non_positive"
    if abs(px - 1.0) < 1e-12:
        return False, "price_sanity:stub_1.0"
    if pair in _FX_PAIRS and _round_price(px) in _FX_STUB_SET:
        # Single tick in the stub ladder is suspicious but only fatal when the
        # whole book matches — handled by price_sanity_book.
        pass
    # Crude scale checks (also used by promote gate).
    if pair in {"EUR/USD", "GBP/USD", "AUD/USD"} and not (0.5 <= px <= 3.0):
        return False, f"price_sanity:fx_scale:{px}"
    if pair == "GBP/JPY" and not (50.0 <= px <= 400.0):
        return False, f"price_sanity:gbpjpy_scale:{px}"
    if pair.startswith("XAU") and not (500.0 <= px <= 10000.0):
        return False, f"price_sanity:xau_scale:{px}"
    if pair.startswith("XAG") and not (5.0 <= px <= 200.0):
        return False, f"price_sanity:xag_scale:{px}"
    if pair.startswith("BTC") and not (1000.0 <= px <= 500000.0):
        return False, f"price_sanity:btc_scale:{px}"
    if pair.startswith("ETH") and not (50.0 <= px <= 50000.0):
        return False, f"price_sanity:eth_scale:{px}"
    return True, ""


def price_sanity_book(
    prices: dict[str, float] | None, price_history: dict[str, list] | None = None
) -> tuple[bool, str]:
    """Detect synthetic FX ladders / cross-pair identical stubs."""
    prices = prices or {}
    if not prices:
        return True, ""
    for pair, px in prices.items():
        ok, reason = price_sanity_pair(pair, px)
        if not ok and "stub_1.0" in reason:
            return False, reason
        if not ok and "scale" in reason:
            return False, reason
    fx_vals = [_round_price(float(prices[p])) for p in prices if p in _FX_PAIRS]
    # Only fatal when the shared value is the known stub ladder / 1.0 (real
    # markets can briefly print similar majors; polluted local HB used 1.1x).
    if len(fx_vals) >= 3 and len(set(fx_vals)) == 1:
        shared = fx_vals[0]
        if shared in _FX_STUB_SET or abs(shared - 1.0) < 1e-12:
            return False, "price_sanity:fx_all_equal_stub"
    # History ladder: each FX pair only uses the known stub set.
    hist = price_history or {}
    stubish = 0
    checked = 0
    for pair in _FX_PAIRS:
        series = hist.get(pair) or []
        if len(series) < 8:
            continue
        checked += 1
        uniq = {_round_price(float(x)) for x in series}
        if uniq and uniq <= _FX_STUB_SET:
            stubish += 1
    if checked >= 2 and stubish == checked:
        return False, "price_sanity:fx_stub_ladder"
    return True, ""


def feed_error_rate(
    skips_path: Path, *, window: int = 200, max_age_s: float | None = None
) -> dict[str, Any]:
    """Fraction of recent skips that are feed/chart failures.

    ``max_age_s`` limits to fresh skips (used by halt recovery so pre-fix
    ``no_candle`` history cannot forever block ``feed_slo`` auto-clear).
    """
    if not skips_path.exists():
        return {"n": 0, "feed_n": 0, "rate": 0.0, "ok": True}
    rows: list[str] = []
    now = time.time()
    try:
        lines = _tail_lines(skips_path, window)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if max_age_s is not None:
                try:
                    ts = float(rec.get("ts") or 0.0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts <= 0 or (now - ts) > max_age_s:
                    continue
            rows.append(str(rec.get("reason") or rec.get("reason_skipped") or ""))
    except OSError:
        return {"n": 0, "feed_n": 0, "rate": 0.0, "ok": True}
    n = len(rows)
    feed_n = sum(1 for r in rows if _is_feed_failure_reason(r))
    rate = (feed_n / n) if n else 0.0
    # Auto-halt threshold: >=40% of last 200 skips are feed failures, n>=40.
    ok = not (n >= 40 and rate >= 0.40)
    return {"n": n, "feed_n": feed_n, "rate": round(rate, 4), "ok": ok}


def idle_skip_slo(skips_path: Path, *, hours: float = 6.0, window: int = 500) -> dict[str, Any]:
    """Detect 'effectively paused': recent skips are all idle/feed with fresh activity."""
    now = time.time()
    cutoff = now - hours * 3600.0
    if not skips_path.exists():
        return {"effectively_paused": False, "detail": "no_skips"}
    reasons: list[str] = []
    recent_ts = 0.0
    try:
        for line in _tail_lines(skips_path, window):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(rec.get("ts") or 0.0)
            if ts < cutoff:
                continue
            recent_ts = max(recent_ts, ts)
            reasons.append(str(rec.get("reason") or ""))
    except OSError as exc:
        return {"effectively_paused": False, "detail": str(exc)}
    if len(reasons) < 20:
        return {"effectively_paused": False, "detail": f"few_recent={len(reasons)}"}
    badish = sum(
        1
        for r in reasons
        if r == "no_signal"
        or r.startswith("no_signal:")
        or _is_feed_failure_reason(r)
        or r.startswith("bb_bandwidth")
    )
    paused = badish == len(reasons)
    return {
        "effectively_paused": paused,
        "detail": f"recent={len(reasons)} badish={badish} last_age={now - recent_ts:.0f}s",
    }


def pair_price_scale_ok(pair: str, price: float) -> bool:
    ok, _ = price_sanity_pair(pair, price)
    return ok


def _tail_lines(path: Path, n: int) -> list[str]:
    """Read last ``n`` lines without loading the whole file when possible."""
    if n <= 0:
        return []
    try:
        # Fast path for moderate files; fall back to full read.
        size = path.stat().st_size
        if size <= 2_000_000:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        # Byte-seek approximate tail for large skip logs.
        with path.open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            block = min(end, max(65_536, n * 200))
            fh.seek(max(0, end - block))
            data = fh.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def rotate_jsonl_if_large(bot: str) -> list[dict[str, Any]]:
    """Rotate skips/gp_shadow when over soak size caps. Returns actions taken."""
    actions: list[dict[str, Any]] = []
    d = bot_state_dir(bot)
    archive = d / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    for name, max_lines in _JSONL_ROTATE_MAX.items():
        path = d / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if len(lines) <= max_lines:
            continue
        keep = _JSONL_ROTATE_KEEP.get(name, max_lines // 4)
        keep_lines = lines[-keep:]
        rotated = lines[:-keep] if keep else lines
        dest = archive / f"{path.stem}_rotated_{stamp}{path.suffix}"
        try:
            dest.write_text("\n".join(rotated) + ("\n" if rotated else ""), encoding="utf-8")
            path.write_text("\n".join(keep_lines) + ("\n" if keep_lines else ""), encoding="utf-8")
            # Aggregate skip counts for monitoring.
            if name == "skips.jsonl":
                counts: dict[str, int] = {}
                for line in rotated:
                    try:
                        rec = json.loads(line)
                        r = str(rec.get("reason") or "unknown")
                        counts[r] = counts.get(r, 0) + 1
                    except json.JSONDecodeError:
                        continue
                (archive / f"skips_counts_{stamp}.json").write_text(
                    json.dumps({"n": len(rotated), "counts": counts}, indent=2),
                    encoding="utf-8",
                )
            actions.append({"file": name, "rotated": len(rotated), "kept": len(keep_lines)})
        except OSError:
            continue
    return actions


def load_open_book(bot: str) -> dict[str, Any]:
    """Load persisted open positions + reentry + mark-fail counters."""
    raw = load_json(open_positions_path(bot), default={}, quarantine=True)
    if not isinstance(raw, dict):
        return {"open_positions": {}, "reentry": {}, "mark_fails": {}, "cycle": 0}
    return {
        "open_positions": dict(raw.get("open_positions") or {}),
        "reentry": dict(raw.get("reentry") or {}),
        "mark_fails": dict(raw.get("mark_fails") or {}),
        "cycle": int(raw.get("cycle") or 0),
        "consecutive_failures": int(raw.get("consecutive_failures") or 0),
    }


def save_open_book(
    bot: str,
    *,
    open_positions: dict,
    reentry: dict,
    mark_fails: dict | None = None,
    cycle: int = 0,
    consecutive_failures: int = 0,
) -> None:
    """Atomically persist the live open book for restart recovery."""
    payload = {
        "ts": time.time(),
        "cycle": int(cycle),
        "consecutive_failures": int(consecutive_failures),
        "open_positions": open_positions or {},
        "reentry": reentry or {},
        "mark_fails": mark_fails or {},
    }
    with contextlib.suppress(OSError):
        atomic_write_json(open_positions_path(bot), payload)


def orphan_force_close_records(
    bot: str,
    open_positions: dict,
    *,
    reason: str = "restart_orphan",
) -> list[dict]:
    """Build close records for positions that cannot be restored cleanly.

    Caller should append these to trades.jsonl then clear the book.
    """
    out: list[dict] = []
    now = time.time()
    for pair, pos in list((open_positions or {}).items()):
        if not isinstance(pos, dict):
            continue
        entry = float(pos.get("entry_price") or 0.0)
        out.append(
            {
                "id": (pos.get("id") or f"{bot}:{pair}:{int(now)}") + ":orphan",
                "bot": bot,
                "pair": pair,
                "cycle": pos.get("held_cycles", 0),
                "reason": reason,
                "exit_reason": reason,
                "entry_type": pos.get("entry_type", "mean_reversion"),
                "strategy_version": pos.get("strategy_version") or pos.get("entry_type"),
                "entry_price": entry,
                "exit_price": entry,
                "entry_ts": pos.get("entry_ts"),
                "exit_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "pnl_pct": 0.0,
                "size": pos.get("size"),
                "hold_cycles": pos.get("held_cycles", 0),
                "orphan": True,
            }
        )
    return out


def append_trade(bot: str, rec: dict) -> bool:
    """Append one trade row; returns False on I/O failure (caller must keep open)."""
    path = bot_state_dir(bot) / "trades.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
        return True
    except OSError:
        return False


def book_drawdown_status(bot: str, goal: dict | None = None) -> dict[str, Any]:
    """Compute cumulative return + peak-to-trough DD from closed trades.

    Units: percent (matches goal.max_drawdown / failure_below in bot config).
    """
    goal = goal or {}
    max_dd = float(goal.get("max_drawdown", 10.0))
    failure_below = float(goal.get("failure_below", -10.0))
    path = bot_state_dir(bot) / "trades.jsonl"
    pnls: list[float] = []
    if path.exists():
        try:
            for line in _tail_lines(path, 5000):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("orphan"):
                    continue
                if rec.get("partial"):
                    # Count partial closes toward book PnL.
                    pass
                try:
                    pnls.append(float(rec.get("pnl_pct", 0.0)))
                except (TypeError, ValueError):
                    continue
        except OSError:
            pnls = []
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_drawdown:
            max_drawdown = dd
    breached_dd = max_drawdown > max_dd and len(pnls) >= 5
    breached_floor = equity <= failure_below and len(pnls) >= 5
    return {
        "n": len(pnls),
        "equity_pct": round(equity, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "max_dd_limit": max_dd,
        "failure_below": failure_below,
        "breached": breached_dd or breached_floor,
        "reason": (
            f"dd_halt:dd={max_drawdown:.2f}>limit={max_dd}"
            if breached_dd
            else (
                f"dd_halt:equity={equity:.2f}<=floor={failure_below}"
                if breached_floor
                else ""
            )
        ),
        "ok": not (breached_dd or breached_floor),
    }


def is_flat_pnl(pnl: float, eps: float = FLAT_PNL_EPS) -> bool:
    return abs(float(pnl)) < float(eps)


def pnl_is_win(pnl: float, eps: float = FLAT_PNL_EPS) -> bool | None:
    """True=win, False=loss, None=flat/neutral."""
    p = float(pnl)
    if abs(p) < float(eps):
        return None
    return p > 0


def append_bb_sample(bot: str, pair: str, bw: float) -> None:
    """Persist BB bandwidth samples for soak measurement (#16)."""
    path = bb_samples_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"ts": time.time(), "pair": pair, "bw": float(bw)}) + "\n"
            )
    except OSError:
        pass


def bb_sample_summary(bot: str, *, window: int = 2000) -> dict[str, Any]:
    path = bb_samples_path(bot)
    vals: list[float] = []
    if path.exists():
        for line in _tail_lines(path, window):
            try:
                rec = json.loads(line)
                vals.append(float(rec["bw"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    if not vals:
        return {"n": 0}
    vals_sorted = sorted(vals)

    def _pct(p: float) -> float:
        if not vals_sorted:
            return 0.0
        i = min(len(vals_sorted) - 1, max(0, int(round((p / 100.0) * (len(vals_sorted) - 1)))))
        return vals_sorted[i]

    return {
        "n": len(vals),
        "p10": round(_pct(10), 6),
        "p50": round(_pct(50), 6),
        "p90": round(_pct(90), 6),
    }
