"""
keep_alive.py – Flask web server + self-ping thread + HTML dashboard.

Routes:
  /          → Live HTML dashboard (auto-refreshes every 10 s)
  /health    → Plain JSON health check
  /stats     → Detailed JSON stats
  /trades    → Recent trade list as JSON
  /symbols   → Symbol leaderboard as JSON

v5 → v6 changes (500-error fix):
  - Embedded HTML dashboard template directly into this file so the server
    never crashes when dashboard.html is missing from the deployment.
  - Wrapped _render_dashboard() in a broad try/except so any unexpected
    rendering error returns a safe fallback page instead of a 500.
  - All existing logic, routes, and helpers are unchanged.
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
    "redeploy_pending":      False,
    "active_trades":         0,
}


def update_status(**kwargs):
    _state.update(kwargs)
    _state["uptime_seconds"] = int(time.time() - _state["start_time"])


def set_redeploy_pending(value: bool) -> None:
    _state["redeploy_pending"] = value
    logger.info(f"redeploy_pending set to {value}")


def is_redeploy_pending() -> bool:
    return bool(_state.get("redeploy_pending", False))


def set_active_trades(count: int) -> None:
    _state["active_trades"] = max(0, int(count))


def get_active_trades() -> int:
    return int(_state.get("active_trades", 0))


def trigger_redeploy() -> None:
    url = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")
    if not url:
        logger.error(
            "trigger_redeploy: RENDER_DEPLOY_HOOK_URL environment variable is "
            "not set — cannot trigger Render redeploy.")
        return
    logger.info("trigger_redeploy: sending POST to Render deploy hook …")
    try:
        resp = requests.post(url, timeout=15)
        if resp.ok:
            logger.info(f"trigger_redeploy: SUCCESS — HTTP {resp.status_code}")
        else:
            logger.error(
                f"trigger_redeploy: FAILED — HTTP {resp.status_code} "
                f"— {resp.text[:300]}")
    except requests.exceptions.Timeout:
        logger.error("trigger_redeploy: FAILED — request timed out after 15 s")
    except requests.exceptions.ConnectionError as exc:
        logger.error(f"trigger_redeploy: FAILED — connection error: {exc}")
    except Exception as exc:
        logger.error(f"trigger_redeploy: FAILED — {type(exc).__name__}: {exc}")


# ── Embedded HTML dashboard template ──────────────────────────────────────────
_EMBEDDED_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>Deriv Bot Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif;
         font-size: 14px; padding: 16px; }
  h1 { font-size: 20px; font-weight: 700; color: var(--accent); margin-bottom: 4px; }
  .subtitle { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px;
          margin-bottom: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
          padding: 14px; }
  .card-title { font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
                color: var(--muted); margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 700; }
  .card-sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .green { color: var(--green); }
  .red   { color: var(--red); }
  .yellow{ color: var(--yellow); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted);
                   text-transform: uppercase; letter-spacing: .05em;
                   margin: 20px 0 8px; }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  th { background: #21262d; color: var(--muted); font-size: 11px; text-transform: uppercase;
       padding: 8px 10px; text-align: left; }
  td { padding: 7px 10px; border-top: 1px solid var(--border); font-size: 12px; }
  .ticker { color: var(--muted); font-size: 11px; }
  .badge { display: inline-block; padding: 2px 6px; border-radius: 4px;
           font-size: 10px; font-weight: 700; }
  .badge-win   { background: #1a3a1f; color: var(--green); }
  .badge-loss  { background: #3a1a1a; color: var(--red); }
  .badge-long  { background: #1a2a3a; color: var(--accent); }
  .badge-short { background: #2a1a3a; color: #c084fc; }
  .status-row { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .dot-green  { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
  .dot-red    { background: var(--red);   box-shadow: 0 0 6px var(--red); }
  .status-text { font-size: 12px; color: var(--muted); }
  .paused-banner { background: #3a1a1a; border: 1px solid var(--red); border-radius: 6px;
                   padding: 10px 14px; margin-bottom: 14px; color: var(--red); font-size: 13px; }
  .bar-wrap { background: #21262d; border-radius: 4px; height: 8px;
              margin-top: 6px; overflow: hidden; }
  .bar-fill  { height: 100%; border-radius: 4px; background: var(--green);
               transition: width .4s; }
  .bar-fill.danger { background: var(--red); }
  .footer { margin-top: 20px; font-size: 11px; color: var(--muted); text-align: right; }
</style>
</head>
<body>

<h1>⚡ Deriv Bot</h1>
<p class="subtitle">Auto-refreshes every 10 s &nbsp;|&nbsp; UTC {{now_utc}}</p>

{{paused_banner}}

<div class="status-row">
  <div class="dot {{dot_class}}"></div>
  <span class="status-text">
    <b>{{session}}</b> &nbsp;·&nbsp; Up {{uptime}}
    &nbsp;·&nbsp; Scanning: <b>{{current_symbol}}</b>
    &nbsp;·&nbsp; Tradeable: <b>{{tradeable_count}}</b>
  </span>
</div>

<div class="grid">
  <div class="card">
    <div class="card-title">Balance</div>
    <div class="card-value {{balance_color}}">${{balance}}</div>
    <div class="card-sub">Start: ${{day_start}}</div>
  </div>
  <div class="card">
    <div class="card-title">Daily P&amp;L</div>
    <div class="card-value {{pnl_color}}">{{daily_pnl_sign}}${{daily_pnl_abs}}</div>
    <div class="card-sub">{{daily_pnl_pct}}%</div>
    <div class="bar-wrap"><div class="bar-fill {{danger_class}}" style="width:{{loss_bar_pct}}%"></div></div>
  </div>
  <div class="card">
    <div class="card-title">Win Rate</div>
    <div class="card-value {{wr_color}}">{{win_rate}}%</div>
    <div class="card-sub">{{wins}}W / {{losses}}L / {{trades}} trades</div>
  </div>
  <div class="card">
    <div class="card-title">Profit Factor</div>
    <div class="card-value {{pf_color}}">{{profit_factor}}</div>
    <div class="card-sub">&nbsp;</div>
  </div>
  <div class="card">
    <div class="card-title">Streak</div>
    <div class="card-value {{streak_color}}">{{streak_label}}</div>
    <div class="card-sub">{{streak_type}}</div>
  </div>
  <div class="card">
    <div class="card-title">Last Signal</div>
    <div class="card-value" style="font-size:13px;">{{last_signal}}</div>
  </div>
</div>

<div class="section-title">Recent Trades</div>
<table>
  <thead>
    <tr>
      <th>Time (UTC)</th><th>Symbol</th><th>Dir</th>
      <th>Stake</th><th>P&amp;L</th><th>Balance</th><th>Result</th>
    </tr>
  </thead>
  <tbody>{{recent_rows}}</tbody>
</table>

<div class="section-title">Symbol Leaderboard</div>
<table>
  <thead>
    <tr><th>Symbol</th><th>Trades</th><th>Win %</th><th>P&amp;L</th><th>Score</th></tr>
  </thead>
  <tbody>{{symbol_rows}}</tbody>
</table>

<div class="footer">Deriv Bot · Render deployment</div>
</body>
</html>"""


def _render_dashboard() -> str:
    try:
        # Try to load external template first; fall back to embedded one
        template = _EMBEDDED_TEMPLATE
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        try:
            with open(path) as f:
                content = f.read()
                if content.strip():
                    template = content
        except Exception:
            pass  # Use embedded template

        s         = _state
        balance   = float(s.get("balance", 0.0))
        day_start = float(s.get("day_start_balance", 0.0))
        daily_pnl = balance - day_start
        pnl_pct   = (daily_pnl / day_start * 100) if day_start else 0
        loss_pct  = max(-pnl_pct, 0)
        loss_bar_pct = min(loss_pct / 90 * 100, 100)

        wins     = int(s.get("wins_today", 0))
        losses   = int(s.get("losses_today", 0))
        trades   = wins + losses
        win_rate = round(wins / trades * 100, 1) if trades else 0
        pf       = float(s.get("profit_factor", 0))
        streak   = int(s.get("streak", 0))

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

        up     = int(s.get("uptime_seconds", 0))
        uptime = f"{up//3600}h {(up%3600)//60}m"

        recent_rows = ""
        for t in reversed(s.get("recent_trades", [])[-20:]):
            try:
                pnl   = float(t.get("pnl", 0))
                won   = bool(t.get("won", False))
                badge = ('<span class="badge badge-win">WIN</span>'  if won else
                         '<span class="badge badge-loss">LOSS</span>')
                dir_b = ('<span class="badge badge-long">LONG</span>'
                         if t.get("direction") == "LONG" else
                         '<span class="badge badge-short">SHORT</span>')
                pnl_c = "green" if pnl >= 0 else "red"
                ts    = str(t.get("exit_time", ""))[:19].replace("T", " ")
                stake = float(t.get("stake", 0))
                bal_a = float(t.get("balance_after", 0))
                recent_rows += (
                    f"<tr>"
                    f"<td class='ticker'>{ts}</td>"
                    f"<td><b>{t.get('symbol','')}</b></td>"
                    f"<td>{dir_b}</td>"
                    f"<td>${stake:.2f}</td>"
                    f"<td class='{pnl_c}'>{'+' if pnl>=0 else ''}{pnl:.4f}</td>"
                    f"<td>${bal_a:.4f}</td>"
                    f"<td>{badge}</td>"
                    f"</tr>"
                )
            except Exception:
                continue
        if not recent_rows:
            recent_rows = ("<tr><td colspan='7' style='text-align:center;color:#484f58'>"
                           "No trades yet</td></tr>")

        symbol_rows = ""
        for sym in s.get("best_symbols", [])[:10]:
            try:
                pnl   = float(sym.get("pnl", 0))
                pnl_c = "green" if pnl >= 0 else "red"
                wr    = float(sym.get("win_rate", 0))
                wr_c  = "green" if wr >= 55 else "yellow" if wr >= 45 else "red"
                symbol_rows += (
                    f"<tr>"
                    f"<td><b>{sym.get('symbol','')}</b></td>"
                    f"<td>{sym.get('trades',0)}</td>"
                    f"<td class='{wr_c}'>{wr}%</td>"
                    f"<td class='{pnl_c}'>${pnl:+.4f}</td>"
                    f"<td class='ticker'>{float(sym.get('score',0)):.3f}</td>"
                    f"</tr>"
                )
            except Exception:
                continue
        if not symbol_rows:
            symbol_rows = ("<tr><td colspan='5' style='text-align:center;color:#484f58'>"
                           "No data yet</td></tr>")

        html = template
        for k, v in {
            "{{dot_class}}":       dot_class,
            "{{session}}":         str(s.get("session", "—")),
            "{{uptime}}":          uptime,
            "{{current_symbol}}":  str(s.get("current_symbol", "—")),
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
            "{{last_signal}}":     str(s.get("last_signal", "—")),
            "{{now_utc}}":         datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "{{recent_rows}}":     recent_rows,
            "{{symbol_rows}}":     symbol_rows,
        }.items():
            html = html.replace(k, str(v))
        return html

    except Exception as exc:
        logger.exception(f"_render_dashboard error: {exc}")
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<meta http-equiv='refresh' content='10'>"
            "<title>Deriv Bot</title></head>"
            "<body style='background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:30px'>"
            f"<h2 style='color:#f85149'>Dashboard render error</h2>"
            f"<pre style='color:#8b949e'>{exc}</pre>"
            "<p>Bot may still be running. Check logs. Page reloads in 10 s.</p>"
            "</body></html>"
        )


@app.route("/")
def index():
    return Response(_render_dashboard(), mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": time.time()}), 200


@app.route("/stats")
def stats_route():
    s         = _state
    balance   = float(s.get("balance", 0.0))
    day_start = float(s.get("day_start_balance", 0.0))
    wins      = int(s.get("wins_today", 0))
    losses    = int(s.get("losses_today", 0))
    trades    = int(s.get("trades_today", wins + losses))
    return jsonify({
        "balance":           round(balance, 4),
        "day_start_balance": round(day_start, 4),
        "daily_pnl":         round(balance - day_start, 4),
        "daily_pnl_pct":     round((balance - day_start) / day_start * 100, 2) if day_start else 0,
        "trades":            trades,
        "wins":              wins,
        "losses":            losses,
        "win_rate":          round(wins / max(trades, 1) * 100, 1),
        "profit_factor":     round(float(s.get("profit_factor", 0)), 3),
        "avg_rr":            round(float(s.get("avg_rr", 0)), 2),
        "streak":            int(s.get("streak", 0)),
        "paused":            bool(s.get("paused_for_loss_limit", False)),
        "current_symbol":    str(s.get("current_symbol", "—")),
        "session":           str(s.get("session", "—")),
        "uptime_seconds":    int(s.get("uptime_seconds", 0)),
        "last_signal":       str(s.get("last_signal", "—")),
        "tradeable_count":   int(s.get("tradeable_count", 0)),
        "active_trades":     int(s.get("active_trades", 0)),
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
    t = threading.Thread(target=_ping_loop, name="keep-alive", daemon=True)
    t.start()
    logger.info(f"Keep-alive pinger started "
                f"(interval={config.KEEP_ALIVE_INTERVAL}s, url={config.SELF_URL})")
