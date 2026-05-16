"""
keep_alive.py – Flask web server + self-ping thread + HTML dashboard.

Routes:
  /          → Live HTML dashboard (auto-refreshes every 10 s)
  /health    → Plain JSON health check
  /stats     → Detailed JSON stats
  /trades    → Recent trade list as JSON
  /symbols   → Symbol leaderboard as JSON

v3 → v4 changes (Bug 2 fix):

  BUG 2 FIX – update_status() now uses dict.update() correctly and
    explicitly initialises every expected key in _state so the dashboard
    never renders a KeyError or missing-value blank on first load.

  BUG 2 FIX – _render_dashboard() now uses .get(key, default) consistently
    for every _state access so a partially-initialised state dict cannot
    cause a KeyError crash that results in the dashboard serving a blank page.

  BUG 2 FIX – The /stats, /trades, /symbols routes now return safe defaults
    when optional keys are absent, preventing 500 errors during the brief
    window between startup and the first _push_dashboard() call.

  No changes to Flask routes, ping loop, or HTML template logic.
"""

import os
import threading
import time
import datetime
import logging
import requests
from flask import Flask, jsonify, Response
import config

logger = logging.getLogger(__name__)
app = Flask(__name__)

# BUG 2 FIX: every key that _render_dashboard() or /stats accesses is
# pre-initialised here with a safe default so the first render never
# encounters a missing key regardless of initialisation order.
_state: dict = {
    "running":               False,
    "balance":               0.0,
    "day_start_balance":     0.0,
    "trades_today":          0,
    "wins_today":            0,
    "losses_today":          0,
    "paused_for_loss_limit": False,
    "current_symbol":        "—",
    "last_signal":           "No signal yet",
    "uptime_seconds":        0,
    "start_time":            time.time(),
    "session":               "Starting …",
    "tradeable_count":       0,
    "gross_profit":          0.0,
    "gross_loss":            0.0,
    "profit_factor":         0.0,
    "avg_rr":                0.0,
    "best_trade":            0.0,
    "worst_trade":           0.0,
    "streak":                0,
    "recent_trades":         [],
    "best_symbols":          [],
    # ── Redeploy coordination ──────────────────────────────────────────────────
    "redeploy_pending":      False,
    "active_trades":         0,
}


def update_status(**kwargs):
    """
    Update the shared dashboard state.
    BUG 2 FIX: uses _state.update() which correctly merges the kwargs dict
    into _state without replacing keys not present in kwargs.
    uptime_seconds is always recalculated from start_time.
    """
    _state.update(kwargs)
    _state["uptime_seconds"] = int(time.time() - _state["start_time"])


# ── Redeploy-coordination helpers ─────────────────────────────────────────────

def set_redeploy_pending(value: bool) -> None:
    """Called by restart_scheduler to pause/resume new trade entries."""
    _state["redeploy_pending"] = value
    logger.info(f"redeploy_pending set to {value}")


def is_redeploy_pending() -> bool:
    """bot_engine calls this before opening any new trade."""
    return bool(_state.get("redeploy_pending", False))


def set_active_trades(count: int) -> None:
    """bot_engine calls this whenever a trade opens or closes."""
    _state["active_trades"] = max(0, int(count))


def get_active_trades() -> int:
    """restart_scheduler polls this to know when all trades have closed."""
    return int(_state.get("active_trades", 0))


_DASH_TEMPLATE: str = ""


def _load_template():
    global _DASH_TEMPLATE
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(path) as f:
            _DASH_TEMPLATE = f.read()
    except Exception:
        _DASH_TEMPLATE = "<html><body><pre>Dashboard template not found.</pre></body></html>"


def _render_dashboard() -> str:
    if not _DASH_TEMPLATE:
        _load_template()

    # BUG 2 FIX: use .get(key, default) for every _state access so a
    # partially-initialised state cannot raise KeyError.
    s         = _state
    balance   = s.get("balance", 0.0)
    day_start = s.get("day_start_balance", 0.0)
    daily_pnl = balance - day_start
    pnl_pct   = (daily_pnl / day_start * 100) if day_start else 0
    loss_pct  = max(-pnl_pct, 0)

    # Progress bar shows percentage of the 90% loss limit consumed
    loss_bar_pct = min(loss_pct / 90 * 100, 100)

    wins     = s.get("wins_today", 0)
    losses   = s.get("losses_today", 0)
    trades   = wins + losses
    win_rate = round(wins / trades * 100, 1) if trades else 0
    pf       = s.get("profit_factor", 0)
    streak   = s.get("streak", 0)

    dot_class    = ("dot-green"  if s.get("running") and not s.get("paused_for_loss_limit")
                    else "dot-yellow" if s.get("paused_for_loss_limit")
                    else "dot-red")
    balance_color = "green" if balance >= day_start else "red"
    pnl_color     = "green" if daily_pnl >= 0 else "red"
    pnl_sign      = "+" if daily_pnl >= 0 else "-"
    wr_color      = "green" if win_rate >= 55 else "yellow" if win_rate >= 45 else "red"
    pf_color      = "green" if pf >= 1.2 else "yellow" if pf >= 1.0 else "red"
    streak_color  = "green" if streak > 0 else "red" if streak < 0 else "yellow"
    streak_label  = str(abs(streak)) if streak else "0"
    streak_type   = "wins" if streak > 0 else "losses" if streak < 0 else "—"
    danger_class  = "danger" if loss_bar_pct > 70 else ""

    paused_banner = ""
    if s.get("paused_for_loss_limit"):
        paused_banner = (
            '<div class="paused-banner">'
            '⛔ Daily loss limit (90%) reached. Trading paused until UTC midnight.'
            '</div>'
        )

    up     = s.get("uptime_seconds", 0)
    uptime = f"{up//3600}h {(up%3600)//60}m"

    recent_rows = ""
    for t in reversed(s.get("recent_trades", [])[-20:]):
        pnl   = t.get("pnl", 0)
        won   = t.get("won", False)
        badge = ('<span class="badge badge-win">WIN</span>'  if won else
                 '<span class="badge badge-loss">LOSS</span>')
        dir_b = ('<span class="badge badge-long">LONG</span>'
                 if t.get("direction") == "LONG" else
                 '<span class="badge badge-short">SHORT</span>')
        pnl_c = "green" if pnl >= 0 else "red"
        ts    = t.get("exit_time", "")[:19].replace("T", " ")
        recent_rows += (
            f"<tr>"
            f"<td class='ticker'>{ts}</td>"
            f"<td><b>{t.get('symbol','')}</b></td>"
            f"<td>{dir_b}</td>"
            f"<td>${t.get('stake', 0):.2f}</td>"
            f"<td class='{pnl_c}'>{'+' if pnl>=0 else ''}{pnl:.4f}</td>"
            f"<td>${t.get('balance_after', 0):.4f}</td>"
            f"<td>{badge}</td>"
            f"</tr>"
        )
    if not recent_rows:
        recent_rows = ("<tr><td colspan='7' style='text-align:center;color:#484f58'>"
                       "No trades yet</td></tr>")

    symbol_rows = ""
    for sym in s.get("best_symbols", [])[:10]:
        pnl   = sym.get("pnl", 0)
        pnl_c = "green" if pnl >= 0 else "red"
        wr    = sym.get("win_rate", 0)
        wr_c  = "green" if wr >= 55 else "yellow" if wr >= 45 else "red"
        symbol_rows += (
            f"<tr>"
            f"<td><b>{sym.get('symbol','')}</b></td>"
            f"<td>{sym.get('trades',0)}</td>"
            f"<td class='{wr_c}'>{wr}%</td>"
            f"<td class='{pnl_c}'>${pnl:+.4f}</td>"
            f"<td class='ticker'>{sym.get('score',0):.3f}</td>"
            f"</tr>"
        )
    if not symbol_rows:
        symbol_rows = ("<tr><td colspan='5' style='text-align:center;color:#484f58'>"
                       "No data yet</td></tr>")

    html = _DASH_TEMPLATE
    for k, v in {
        "{{dot_class}}":       dot_class,
        "{{session}}":         s.get("session", "—"),
        "{{uptime}}":          uptime,
        "{{current_symbol}}":  s.get("current_symbol", "—"),
        "{{paused_banner}}":   paused_banner,
        "{{balance}}":         f"{balance:.4f}",
        "{{balance_color}}":   balance_color,
        "{{day_start}}":       f"{day_start:.4f}",
        "{{daily_pnl_sign}}":  pnl_sign,
        "{{daily_pnl_abs}}":   f"{abs(daily_pnl):.4f}",
        "{{daily_pnl_pct}}":   f"{pnl_pct:+.2f}",
        "{{pnl_color}}":       pnl_color,
        "{{loss_bar_pct}}":    f"{loss_bar_pct:.0f}",
        "{{danger_class}}":    danger_class,
        "{{win_rate}}":        f"{win_rate:.1f}",
        "{{wr_color}}":        wr_color,
        "{{wins}}":            str(wins),
        "{{losses}}":          str(losses),
        "{{trades}}":          str(trades),
        "{{profit_factor}}":   f"{pf:.3f}",
        "{{pf_color}}":        pf_color,
        "{{streak_label}}":    streak_label,
        "{{streak_color}}":    streak_color,
        "{{streak_type}}":     streak_type,
        "{{tradeable_count}}": str(s.get("tradeable_count", 0)),
        "{{last_signal}}":     s.get("last_signal", "—"),
        "{{now_utc}}":         datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "{{recent_rows}}":     recent_rows,
        "{{symbol_rows}}":     symbol_rows,
    }.items():
        html = html.replace(k, str(v))
    return html


@app.route("/")
def index():
    return Response(_render_dashboard(), mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": time.time()}), 200


@app.route("/stats")
def stats_route():
    # BUG 2 FIX: use .get() with defaults so /stats never 500s during init
    s         = _state
    balance   = s.get("balance", 0.0)
    day_start = s.get("day_start_balance", 0.0)
    wins      = s.get("wins_today", 0)
    losses    = s.get("losses_today", 0)
    trades    = s.get("trades_today", wins + losses)
    return jsonify({
        "balance":           round(balance, 4),
        "day_start_balance": round(day_start, 4),
        "daily_pnl":         round(balance - day_start, 4),
        "daily_pnl_pct":     round((balance - day_start) / day_start * 100, 2) if day_start else 0,
        "trades":            trades,
        "wins":              wins,
        "losses":            losses,
        "win_rate":          round(wins / max(trades, 1) * 100, 1),
        "profit_factor":     round(s.get("profit_factor", 0), 3),
        "avg_rr":            round(s.get("avg_rr", 0), 2),
        "streak":            s.get("streak", 0),
        "paused":            s.get("paused_for_loss_limit", False),
        "current_symbol":    s.get("current_symbol", "—"),
        "session":           s.get("session", "—"),
        "uptime_seconds":    s.get("uptime_seconds", 0),
        "last_signal":       s.get("last_signal", "—"),
        "tradeable_count":   s.get("tradeable_count", 0),
        "active_trades":     s.get("active_trades", 0),
    })


@app.route("/trades")
def trades_route():
    return jsonify({"recent_trades": _state.get("recent_trades", [])})


@app.route("/symbols")
def symbols_route():
    return jsonify({"symbols": _state.get("best_symbols", [])})


def _ping_loop():
    time.sleep(20)
    while True:
        try:
            r = requests.get(f"{config.SELF_URL}/health", timeout=10)
            logger.debug(f"Keep-alive ping → {r.status_code}")
        except Exception as exc:
            logger.warning(f"Keep-alive ping failed: {exc}")
        time.sleep(config.KEEP_ALIVE_INTERVAL)


def start_keep_alive():
    _load_template()
    t = threading.Thread(target=_ping_loop, name="keep-alive", daemon=True)
    t.start()
    logger.info(f"Keep-alive pinger started "
                f"(interval={config.KEEP_ALIVE_INTERVAL}s, url={config.SELF_URL})")
