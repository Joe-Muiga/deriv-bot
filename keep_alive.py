"""
keep_alive.py – Flask web server + self-ping keep-alive thread.

Hardened against 500 errors:
  • Every route wraps bot-state access in try/except
  • Missing attributes fall back to safe defaults (0, [], "N/A", False)
  • Dashboard never returns 5xx — catches all exceptions, returns 200
  • /health always returns 200 regardless of bot state
  • Template fallback: if dashboard.html is missing, renders an inline status page
  • All other routes follow the same safety pattern
"""

import logging
import threading
import time

import requests
from flask import Flask, jsonify, render_template, render_template_string
from flask import current_app

import config

logger = logging.getLogger("keep_alive")

app = Flask(__name__)

# ─── Bot reference ─────────────────────────────────────────────────────────────
# Populated by bot_engine after initialisation so routes can read live state.
# Always use getattr() with a default — never assume the attribute exists.
_bot_instance = None


def register_bot(bot):
    """Called by BotEngine once it is initialised so the dashboard can read state."""
    global _bot_instance
    _bot_instance = bot
    logger.info("Bot instance registered with Flask dashboard ✓")


# ─── Safe state helper ─────────────────────────────────────────────────────────

def _safe_bot_context():
    """
    Return a dict of bot state values, with safe defaults for every field.
    Never raises — if anything goes wrong a fallback dict is returned.
    """
    bot = _bot_instance
    try:
        # Risk sub-object
        win_streak  = 0
        loss_streak = 0
        stake       = 0.0
        if bot is not None and hasattr(bot, "risk") and bot.risk is not None:
            win_streak  = getattr(bot.risk, "win_streak",  0)
            loss_streak = getattr(bot.risk, "loss_streak", 0)
            stake       = getattr(bot.risk, "current_stake", 0.0)

        # Queue / symbol list — _queue may be a deque or list
        raw_queue = getattr(bot, "_queue", []) if bot is not None else []
        try:
            active_symbols = list(raw_queue)
        except Exception:
            active_symbols = []

        # Open contracts — may be dict or similar mapping
        raw_contracts = getattr(bot, "_open_contracts", {}) if bot is not None else {}
        try:
            open_trades = dict(raw_contracts)
        except Exception:
            open_trades = {}

        # Trade history
        raw_history = getattr(bot, "_trade_history", []) if bot is not None else []
        try:
            trade_history = list(raw_history)[-50:]   # last 50 trades max
        except Exception:
            trade_history = []

        return {
            "balance":        round(float(getattr(bot, "current_balance", 0.0) if bot else 0.0), 2),
            "cycle":          int(getattr(bot,   "_cycle_count",  0) if bot else 0),
            "active_symbols": active_symbols,
            "symbol_count":   len(active_symbols),
            "open_trades":    open_trades,
            "open_count":     len(open_trades),
            "win_streak":     win_streak,
            "loss_streak":    loss_streak,
            "stake":          round(float(stake), 4),
            "running":        bool(getattr(bot, "_running", False) if bot else False),
            "total_profit":   round(float(getattr(bot, "_total_profit", 0.0) if bot else 0.0), 2),
            "total_trades":   int(getattr(bot, "_total_trades", 0) if bot else 0),
            "wins":           int(getattr(bot, "_wins",  0) if bot else 0),
            "losses":         int(getattr(bot, "_losses", 0) if bot else 0),
            "trade_history":  trade_history,
            "app_id":         config.DERIV_APP_ID,
            "bot_initialised": bot is not None,
        }
    except Exception as exc:
        logger.warning(f"_safe_bot_context() fallback triggered: {exc}")
        return {
            "balance": 0.0, "cycle": 0, "active_symbols": [], "symbol_count": 0,
            "open_trades": {}, "open_count": 0, "win_streak": 0, "loss_streak": 0,
            "stake": 0.0, "running": False, "total_profit": 0.0, "total_trades": 0,
            "wins": 0, "losses": 0, "trade_history": [], "app_id": "N/A",
            "bot_initialised": False,
        }


# ─── Inline fallback dashboard ─────────────────────────────────────────────────
_FALLBACK_DASHBOARD = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <title>SIFM Bot – Status</title>
  <style>
    :root {
      --bg: #0d1117; --surface: #161b22; --border: #30363d;
      --green: #3fb950; --red: #f85149; --amber: #d29922;
      --blue: #58a6ff; --text: #c9d1d9; --muted: #8b949e;
      --font: 'Courier New', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: var(--font);
           min-height: 100vh; padding: 2rem; }
    h1   { color: var(--blue); font-size: 1.4rem; margin-bottom: .25rem; }
    .sub { color: var(--muted); font-size: .8rem; margin-bottom: 2rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 1rem; margin-bottom: 2rem; }
    .card { background: var(--surface); border: 1px solid var(--border);
            border-radius: 6px; padding: 1rem; }
    .card .label { font-size: .7rem; text-transform: uppercase;
                   letter-spacing: .08em; color: var(--muted); margin-bottom: .3rem; }
    .card .value { font-size: 1.5rem; font-weight: bold; }
    .green { color: var(--green); } .red { color: var(--red); }
    .amber { color: var(--amber); } .blue { color: var(--blue); }
    .badge { display: inline-block; padding: .15rem .5rem; border-radius: 4px;
             font-size: .75rem; font-weight: bold; }
    .badge.on  { background: #1a4731; color: var(--green); }
    .badge.off { background: #3d1e1e; color: var(--red); }
    table { width: 100%; border-collapse: collapse; font-size: .8rem; }
    th, td { padding: .45rem .7rem; border-bottom: 1px solid var(--border); text-align: left; }
    th { color: var(--muted); text-transform: uppercase; font-size: .7rem; }
    .section-title { color: var(--muted); font-size: .75rem; text-transform: uppercase;
                     letter-spacing: .1em; margin: 1.5rem 0 .5rem; }
    .note { color: var(--amber); font-size: .75rem; margin-top: 1rem; }
  </style>
</head>
<body>
  <h1>⚡ SIFM Deriv Trading Bot</h1>
  <div class="sub">App ID: {{ app_id }} &nbsp;|&nbsp; Auto-refreshes every 15 s</div>

  <div class="grid">
    <div class="card">
      <div class="label">Status</div>
      <div class="value">
        {% if running %}
          <span class="badge on">● RUNNING</span>
        {% elif bot_initialised %}
          <span class="badge off">■ STOPPED</span>
        {% else %}
          <span class="badge off">⌛ INIT…</span>
        {% endif %}
      </div>
    </div>
    <div class="card">
      <div class="label">Balance</div>
      <div class="value blue">${{ "%.2f"|format(balance) }}</div>
    </div>
    <div class="card">
      <div class="label">Total P&L</div>
      <div class="value {% if total_profit >= 0 %}green{% else %}red{% endif %}">
        {% if total_profit >= 0 %}+{% endif %}${{ "%.2f"|format(total_profit) }}
      </div>
    </div>
    <div class="card">
      <div class="label">Cycle</div>
      <div class="value">{{ cycle }}</div>
    </div>
    <div class="card">
      <div class="label">Trades</div>
      <div class="value">{{ total_trades }}</div>
    </div>
    <div class="card">
      <div class="label">W / L</div>
      <div class="value"><span class="green">{{ wins }}</span> / <span class="red">{{ losses }}</span></div>
    </div>
    <div class="card">
      <div class="label">Open Trades</div>
      <div class="value amber">{{ open_count }}</div>
    </div>
    <div class="card">
      <div class="label">Symbols</div>
      <div class="value">{{ symbol_count }}</div>
    </div>
    <div class="card">
      <div class="label">Win Streak</div>
      <div class="value green">{{ win_streak }}</div>
    </div>
    <div class="card">
      <div class="label">Loss Streak</div>
      <div class="value red">{{ loss_streak }}</div>
    </div>
    <div class="card">
      <div class="label">Current Stake</div>
      <div class="value">${{ "%.4f"|format(stake) }}</div>
    </div>
  </div>

  {% if active_symbols %}
  <div class="section-title">Active Symbols ({{ symbol_count }})</div>
  <table>
    <thead><tr><th>#</th><th>Symbol</th></tr></thead>
    <tbody>
      {% for sym in active_symbols %}
      <tr><td>{{ loop.index }}</td><td>{{ sym }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  {% if open_trades %}
  <div class="section-title">Open Contracts</div>
  <table>
    <thead><tr><th>Contract ID</th><th>Detail</th></tr></thead>
    <tbody>
      {% for cid, detail in open_trades.items() %}
      <tr><td>{{ cid }}</td><td>{{ detail }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  {% if trade_history %}
  <div class="section-title">Recent Trades (last {{ trade_history|length }})</div>
  <table>
    <thead><tr>
      <th>#</th><th>Symbol</th><th>Direction</th>
      <th>Stake</th><th>P&L</th><th>Result</th>
    </tr></thead>
    <tbody>
      {% for t in trade_history|reverse %}
      <tr>
        <td>{{ loop.index }}</td>
        <td>{{ t.get('symbol',   'N/A') }}</td>
        <td>{{ t.get('direction','N/A') }}</td>
        <td>${{ "%.4f"|format(t.get('stake', 0)) }}</td>
        <td class="{% if t.get('profit',0) >= 0 %}green{% else %}red{% endif %}">
          {% if t.get('profit',0) >= 0 %}+{% endif %}${{ "%.4f"|format(t.get('profit', 0)) }}
        </td>
        <td class="{% if t.get('won') %}green{% else %}red{% endif %}">
          {% if t.get('won') %}WIN{% else %}LOSS{% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <p class="note">⚠ Fallback dashboard — place dashboard.html in templates/ for a custom UI.</p>
</body>
</html>
"""


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """Always returns 200 — used by Render health checks and the self-pinger."""
    return jsonify({"status": "ok"}), 200


@app.route("/")
def dashboard():
    """
    Main dashboard. NEVER returns 5xx.
    Tries dashboard.html first; falls back to the inline template on any error.
    """
    try:
        ctx = _safe_bot_context()
        try:
            return render_template("dashboard.html", **ctx)
        except Exception as tpl_err:
            logger.warning(f"dashboard.html not found or errored ({tpl_err}); using fallback template")
            return render_template_string(_FALLBACK_DASHBOARD, **ctx), 200
    except Exception as exc:
        logger.error(f"Dashboard route fatal error: {exc}", exc_info=True)
        return (
            "<h2 style='font-family:monospace;padding:2rem'>"
            "⚡ SIFM Bot is starting up…</h2>"
            f"<pre style='padding:2rem'>{exc}</pre>"
        ), 200


@app.route("/status")
def status():
    """JSON status endpoint — safe defaults, never 5xx."""
    try:
        ctx = _safe_bot_context()
        # Remove heavy list fields to keep the JSON lean
        ctx.pop("trade_history", None)
        return jsonify(ctx), 200
    except Exception as exc:
        logger.error(f"/status error: {exc}", exc_info=True)
        return jsonify({"error": str(exc), "status": "degraded"}), 200


@app.route("/trades")
def trades():
    """Return recent trade history as JSON. Safe, never 5xx."""
    try:
        bot = _bot_instance
        raw = getattr(bot, "_trade_history", []) if bot is not None else []
        history = list(raw)[-100:]
        return jsonify({"count": len(history), "trades": history}), 200
    except Exception as exc:
        logger.error(f"/trades error: {exc}", exc_info=True)
        return jsonify({"error": str(exc), "trades": []}), 200


@app.route("/symbols")
def symbols():
    """Return active symbols as JSON. Safe, never 5xx."""
    try:
        bot = _bot_instance
        raw = getattr(bot, "_queue", []) if bot is not None else []
        sym_list = list(raw)
        return jsonify({"count": len(sym_list), "symbols": sym_list}), 200
    except Exception as exc:
        logger.error(f"/symbols error: {exc}", exc_info=True)
        return jsonify({"error": str(exc), "symbols": []}), 200


@app.route("/ping")
def ping():
    """Lightweight liveness check for the self-pinger."""
    return "pong", 200


# ─── Self-ping keep-alive ──────────────────────────────────────────────────────

def _ping_loop():
    """Pings /health every 40 s to prevent Render from spinning down the service."""
    url = f"{config.SELF_URL}/health"
    while True:
        try:
            r = requests.get(url, timeout=10)
            logger.debug(f"Self-ping {url} → {r.status_code}")
        except Exception as exc:
            logger.warning(f"Self-ping failed: {exc}")
        time.sleep(40)


def start_keep_alive():
    """Start the self-ping thread (daemon so it dies with the process)."""
    t = threading.Thread(target=_ping_loop, name="keep-alive-pinger", daemon=True)
    t.start()
    logger.info(f"Keep-alive pinger started → {config.SELF_URL}/health every 40 s ✓")
