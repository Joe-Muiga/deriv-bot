"""
keep_alive.py – Flask web server + self-ping thread + HTML dashboard.

Routes:
  /          → Live HTML dashboard (auto-refreshes every 10 s)
  /health    → Plain JSON health check
  /stats     → Detailed JSON stats
  /trades    → Recent trade list as JSON
  /symbols   → Symbol leaderboard as JSON

v8 changes:
  - Dashboard fully rebuilt with pure Python f-strings. Zero {{variable}} syntax anywhere.
  - Replaced Chart.js CDN dependency with pure inline SVG balance curve.
  - _state updated to match canonical field names used by update_status().
  - update_status() stores all canonical fields.
  - All other logic, routes, ping loop, and helpers are unchanged.

v9 changes:
  - Balance curve now correctly reads balance_history (populated by record_trade).
  - _parse_hms() added to handle HH:MM:SS time strings from record_trade.
  - record_signal() now stores a dict so the dashboard renders symbol/direction
    badges and score separately instead of a flat string.
  - Dashboard verified: balance, daily PnL ($+%), win rate, streak, total trades
    today, last signal (with inverted direction badge), recent trades table,
    suspended symbols with minutes remaining, session status — all live on
    every 10 s refresh.
  - _build_svg_chart() rewritten to consume balance_history directly.
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
    "daily_pnl":             0.0,
    "daily_pnl_pct":         0.0,
    "win_rate":              0.0,
    "wins":                  0,
    "losses":                0,
    "total_trades":          0,
    "streak":                0,
    "streak_label":          "—",
    "session":               "Starting",
    "tradeable_count":       0,
    "last_signal":           "No signal yet",
    "recent_trades":         [],
    "best_symbols":          [],
    "balance_history":       [],
    "suspended_symbols":     [],
    "paused_for_loss_limit": False,
    # Legacy / internal fields kept for compatibility
    "current_symbol":        "—",
    "uptime_seconds":        0,
    "start_time":            time.time(),
    "gross_profit":          0.0,
    "gross_loss":            0.0,
    "profit_factor":         0.0,
    "avg_rr":                0.0,
    "best_trade":            0.0,
    "worst_trade":           0.0,
    "redeploy_pending":      False,
    "active_trades":         0,
}


# Alias so bot_engine can import _status and reference the same dict
_status = _state


def update_status(**kwargs) -> None:
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


def record_trade(symbol, direction, stake, pnl,
                 balance_after, won, strategy=""):
    """Called directly after every trade closes."""
    from datetime import datetime, timezone

    trade = {
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "symbol": symbol,
        "direction": direction,
        "stake": round(stake, 4),
        "pnl": round(pnl, 4),
        "balance_after": round(balance_after, 4),
        "won": won,
        "strategy": strategy,
    }

    # Add to recent trades — keep last 50
    _status["recent_trades"].insert(0, trade)
    _status["recent_trades"] = _status["recent_trades"][:50]

    # Add to balance history for chart — keep last 100
    _status["balance_history"].append({
        "time": trade["time"],          # "HH:MM:SS"
        "balance": balance_after,
        "won": won,
    })
    _status["balance_history"] = _status["balance_history"][-100:]

    # Update win/loss counts
    if won:
        _status["wins"] += 1
    else:
        _status["losses"] += 1
    _status["total_trades"] = _status["wins"] + _status["losses"]

    # Recalculate win rate
    if _status["total_trades"] > 0:
        _status["win_rate"] = round(
            _status["wins"] / _status["total_trades"] * 100, 1)

    # Update daily PnL
    day_start = _status.get("day_start_balance", balance_after)
    if day_start > 0:
        _status["daily_pnl"] = round(balance_after - day_start, 4)
        _status["daily_pnl_pct"] = round(
            (_status["daily_pnl"] / day_start) * 100, 2)

    _status["balance"] = balance_after


def record_signal(symbol, direction, strategy, score):
    """
    Store last signal as a structured dict so the dashboard can render
    the direction as a colour-coded badge and display score precisely.
    The direction passed here must already be the INVERTED (placed) direction
    — signal_engine applies contrarian inversion before returning.
    """
    _status["last_signal"] = {
        "symbol":    symbol,
        "direction": direction,
        "strategy":  strategy,
        "score":     round(float(score), 3),
    }


def update_suspended_symbols(suspended: list) -> None:
    """
    Update the list of suspended symbols.
    Each entry: {"symbol": str, "suspended_until": float (unix timestamp)}.
    """
    _state["suspended_symbols"] = list(suspended)


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


# ── Time helpers ───────────────────────────────────────────────────────────────

def _exit_time_to_dt(raw) -> datetime.datetime | None:
    """Parse exit_time regardless of whether it's a Unix timestamp or ISO string."""
    if raw is None:
        return None
    try:
        f = float(raw)
        if f > 1_000_000_000:
            return datetime.datetime.utcfromtimestamp(f)
    except (TypeError, ValueError):
        pass
    s = str(raw).replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_hms(raw) -> str:
    """
    Extract HH:MM display label from a time value.
    Handles:
      - "HH:MM:SS" strings produced by record_trade  → slices to "HH:MM"
      - Unix epoch floats / ints                      → formats as "HH:MM"
      - ISO datetime strings                          → formats as "HH:MM"
    Returns "?" on failure.
    """
    if raw is None:
        return "?"
    s = str(raw).strip()
    # "HH:MM:SS" or "HH:MM" — direct time string
    if len(s) >= 5 and s[2] == ":":
        return s[:5]
    # Try Unix epoch
    try:
        f = float(s)
        if f > 1_000_000_000:
            return datetime.datetime.utcfromtimestamp(f).strftime("%H:%M")
    except (TypeError, ValueError):
        pass
    # Try ISO datetime
    dt = _exit_time_to_dt(raw)
    if dt:
        return dt.strftime("%H:%M")
    return "?"


# ── SVG Balance Curve ──────────────────────────────────────────────────────────

def _build_svg_chart(balance_history: list) -> str:
    """
    Build a pure inline SVG balance curve from balance_history entries.
    Each entry: {"time": "HH:MM:SS", "balance": float, "won": bool}
    Produced by record_trade() — no external dependencies.

    Chart title: "Balance Curve — Today"  (rendered in caller)
    X axis: trade timestamps as HH:MM
    Y axis: balance after each trade
    Green dots = winning trades, red dots = losing trades
    Updates on every dashboard refresh (10 s).
    """
    W, H = 900, 180
    PAD_L, PAD_R, PAD_T, PAD_B = 58, 12, 12, 32

    # Build point list from balance_history
    points = []
    for entry in balance_history:
        try:
            bal  = float(entry.get("balance") or entry.get("balance_after") or 0)
            if bal == 0:
                continue
            won   = bool(entry.get("won", False))
            label = _parse_hms(entry.get("time") or entry.get("exit_time"))
            points.append({"t": label, "bal": bal, "won": won})
        except Exception:
            continue

    if not points:
        return (
            f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:180px;">'
            f'<text x="{W // 2}" y="{H // 2}" text-anchor="middle" '
            f'fill="#484f58" font-size="13" font-family="Segoe UI,sans-serif">'
            f'No trades yet — chart will populate after first closed trade'
            f'</text>'
            f'</svg>'
        )

    bals   = [p["bal"] for p in points]
    min_b  = min(bals)
    max_b  = max(bals)
    span   = max_b - min_b if max_b != min_b else 1.0

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B
    n      = len(points)

    def px(i):
        return PAD_L + (i / max(n - 1, 1)) * plot_w

    def py(b):
        return PAD_T + plot_h - ((b - min_b) / span) * plot_h

    # Polyline
    coords = " ".join(
        f"{px(i):.1f},{py(p['bal']):.1f}" for i, p in enumerate(points)
    )
    line = (
        f'<polyline points="{coords}" fill="none" stroke="#58a6ff" '
        f'stroke-width="2" stroke-linejoin="round"/>'
    )

    # Coloured dots — green=win, red=loss
    dots = ""
    for i, p in enumerate(points):
        fill = "#3fb950" if p["won"] else "#f85149"
        dots += (
            f'<circle cx="{px(i):.1f}" cy="{py(p["bal"]):.1f}" r="4" '
            f'fill="{fill}" stroke="#0d1117" stroke-width="1.5">'
            f'<title>{p["t"]}  ${p["bal"]:.4f}</title>'
            f'</circle>'
        )

    # Y-axis labels (3 ticks)
    y_labels = ""
    for tick in [0, 0.5, 1.0]:
        val = min_b + tick * span
        y   = PAD_T + plot_h - tick * plot_h
        y_labels += (
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
            f'stroke="#21262d" stroke-width="1"/>'
            f'<text x="{PAD_L - 4}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="#8b949e" font-size="10" font-family="Segoe UI,sans-serif">'
            f'${val:.2f}</text>'
        )

    # X-axis labels (up to 6 evenly spaced)
    x_labels = ""
    step = max(1, n // 6)
    for i in range(0, n, step):
        x = px(i)
        x_labels += (
            f'<text x="{x:.1f}" y="{H - 4}" text-anchor="middle" '
            f'fill="#8b949e" font-size="10" font-family="Segoe UI,sans-serif">'
            f'{points[i]["t"]}</text>'
        )

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:180px;">'
        f'{y_labels}{line}{dots}{x_labels}'
        f'</svg>'
    )


# ── Dashboard renderer ─────────────────────────────────────────────────────────

def _render_dashboard() -> str:
    try:
        s = _state

        # ── Computed values ───────────────────────────────────────────────────
        balance   = float(s.get("balance", 0.0))
        day_start = float(s.get("day_start_balance", 0.0))
        daily_pnl = balance - day_start
        pnl_pct   = (daily_pnl / day_start * 100) if day_start else 0.0
        loss_pct  = max(-pnl_pct, 0.0)
        loss_bar  = min(loss_pct / 15.0 * 100, 100)   # 15 % daily loss limit

        wins      = int(s.get("wins", s.get("wins_today", 0)))
        losses    = int(s.get("losses", s.get("losses_today", 0)))
        trades    = int(s.get("total_trades", wins + losses))
        win_rate  = round(wins / trades * 100, 1) if trades else 0.0
        streak    = int(s.get("streak", 0))
        streak_lbl = str(s.get("streak_label", "—"))

        pf        = float(s.get("profit_factor", 0.0))

        dot_class     = ("dot-green"  if s.get("running") and not s.get("paused_for_loss_limit")
                         else "dot-yellow" if s.get("paused_for_loss_limit")
                         else "dot-red")
        balance_color = "green" if balance >= day_start else "red"
        pnl_color     = "green" if daily_pnl >= 0 else "red"
        pnl_sign      = "+" if daily_pnl >= 0 else "−"
        wr_color      = "green" if win_rate >= 55 else "yellow" if win_rate >= 45 else "red"
        pf_color      = "green" if pf >= 1.2 else "yellow" if pf >= 1.0 else "red"
        streak_color  = "green" if streak > 0 else "red" if streak < 0 else "yellow"
        streak_disp   = f"+{streak}" if streak > 0 else str(streak) if streak < 0 else "0"
        danger_class  = "danger" if loss_bar > 70 else ""

        up     = int(s.get("uptime_seconds", 0))
        uptime = f"{up // 3600}h {(up % 3600) // 60}m"
        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        # ── Paused banner ─────────────────────────────────────────────────────
        paused_banner = ""
        if s.get("paused_for_loss_limit"):
            paused_banner = (
                '<div class="paused-banner">'
                '⛔ Daily loss limit (15%) reached. Trading paused until UTC midnight.'
                '</div>'
            )

        # ── Last signal ───────────────────────────────────────────────────────
        # record_signal() now always stores a dict; handle legacy string too.
        raw_sig = s.get("last_signal", "No signal yet")
        if isinstance(raw_sig, dict):
            sig_sym   = str(raw_sig.get("symbol", "—"))
            sig_dir   = str(raw_sig.get("direction", "—"))
            sig_strat = str(raw_sig.get("strategy", "—"))
            sig_score = raw_sig.get("score", None)
            dir_badge = (
                '<span class="badge badge-long">LONG</span>'   if sig_dir == "LONG" else
                '<span class="badge badge-short">SHORT</span>' if sig_dir == "SHORT" else
                sig_dir
            )
            last_sig_sym    = f"{sig_sym} {dir_badge}"
            last_sig_detail = sig_strat + (f" · score {float(sig_score):.3f}" if sig_score is not None else "")
        else:
            last_sig_sym    = str(raw_sig)
            last_sig_detail = ""

        # ── Recent trades table ───────────────────────────────────────────────
        recent_trades_list = s.get("recent_trades", [])
        trade_rows = ""
        bad_rows = 0
        for t in recent_trades_list[:20]:   # already newest-first from insert(0,...)
            try:
                pnl_raw = t.get("pnl") if t.get("pnl") is not None else t.get("profit", t.get("return", 0))
                pnl   = float(pnl_raw or 0)
                won   = bool(t.get("won", pnl > 0))
                badge = ('<span class="badge badge-win">WIN</span>' if won
                         else '<span class="badge badge-loss">LOSS</span>')
                raw_dir = str(t.get("direction", t.get("contract_type", "")) or "").upper()
                if "LONG" in raw_dir or "CALL" in raw_dir:
                    dir_b = '<span class="badge badge-long">LONG</span>'
                elif "SHORT" in raw_dir or "PUT" in raw_dir:
                    dir_b = '<span class="badge badge-short">SHORT</span>'
                else:
                    dir_b = f'<span class="ticker">{raw_dir or "—"}</span>'
                pnl_c  = "green" if pnl >= 0 else "red"
                # Timestamp: prefer exit_time/close_time; fall back to "time" field from record_trade
                ts_raw = t.get("exit_time") or t.get("close_time") or t.get("time")
                ts_str = _parse_hms(ts_raw) if ts_raw else "—"
                stake_raw = t.get("stake") if t.get("stake") is not None else t.get("amount", t.get("buy_price", 0))
                stake = float(stake_raw or 0)
                bal_raw = t.get("balance_after") if t.get("balance_after") is not None else t.get("balance", 0)
                bal_a = float(bal_raw or 0)
                sym   = t.get("symbol", t.get("market", ""))
                strat = t.get("strategy", "")
                trade_rows += (
                    f"<tr>"
                    f"<td class='ticker'>{ts_str}</td>"
                    f"<td><b>{sym}</b></td>"
                    f"<td>{dir_b}</td>"
                    f"<td>${stake:.2f}</td>"
                    f"<td class='{pnl_c}'>{'+' if pnl >= 0 else ''}{pnl:.4f}</td>"
                    f"<td>${bal_a:.4f}</td>"
                    f"<td>{badge}</td>"
                    f"</tr>"
                )
            except Exception as row_err:
                bad_rows += 1
                logger.warning(f"trade row skipped — {row_err} — data={t}")
                continue
        if not trade_rows:
            detail = (f" ({len(recent_trades_list)} in state, {bad_rows} parse errors)"
                      if recent_trades_list else "")
            trade_rows = (f"<tr><td colspan='7' style='text-align:center;color:#484f58'>"
                          f"No trades yet{detail}</td></tr>")

        # ── Suspended symbols ─────────────────────────────────────────────────
        now_ts = time.time()
        active_susp = [
            x for x in s.get("suspended_symbols", [])
            if float(x.get("suspended_until", 0)) > now_ts
        ]
        susp_rows = ""
        for x in sorted(active_susp, key=lambda z: float(z.get("suspended_until", 0))):
            mins_left = max(0, int((float(x["suspended_until"]) - now_ts) / 60))
            susp_rows += (
                f"<tr>"
                f"<td><b>{x.get('symbol', '—')}</b></td>"
                f"<td><span class='susp-badge'>⏸ {mins_left} min remaining</span></td>"
                f"</tr>"
            )
        if not susp_rows:
            susp_rows = ("<tr><td colspan='2' style='text-align:center;color:#484f58'>"
                         "None suspended</td></tr>")

        # ── Symbol leaderboard ────────────────────────────────────────────────
        sym_rows = ""
        for sym in s.get("best_symbols", [])[:10]:
            try:
                pnl   = float(sym.get("pnl", 0))
                pnl_c = "green" if pnl >= 0 else "red"
                wr    = float(sym.get("win_rate", 0))
                wr_c  = "green" if wr >= 55 else "yellow" if wr >= 45 else "red"
                sym_rows += (
                    f"<tr>"
                    f"<td><b>{sym.get('symbol','')}</b></td>"
                    f"<td>{sym.get('trades', 0)}</td>"
                    f"<td class='{wr_c}'>{wr}%</td>"
                    f"<td class='{pnl_c}'>${pnl:+.4f}</td>"
                    f"<td class='ticker'>{float(sym.get('score', 0)):.3f}</td>"
                    f"</tr>"
                )
            except Exception:
                continue
        if not sym_rows:
            sym_rows = ("<tr><td colspan='5' style='text-align:center;color:#484f58'>"
                        "No data yet</td></tr>")

        # ── SVG balance curve — fed from balance_history ───────────────────────
        # balance_history is the purpose-built list populated by record_trade().
        # Falls back to empty list (shows "No trades yet" placeholder).
        svg_chart = _build_svg_chart(s.get("balance_history", []))

        # ── Session / queue info ──────────────────────────────────────────────
        session      = str(s.get("session", "Starting"))
        queue_count  = int(s.get("tradeable_count", 0))
        current_sym  = str(s.get("current_symbol", "—"))

        # ── Render ────────────────────────────────────────────────────────────
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>Deriv Bot Dashboard</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text);
          font-family: 'Segoe UI', sans-serif; font-size: 14px; padding: 16px; }}
  h1 {{ font-size: 20px; font-weight: 700; color: var(--accent); margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 12px; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
           gap: 12px; margin-bottom: 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--border);
           border-radius: 8px; padding: 14px; }}
  .card-title {{ font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
                 color: var(--muted); margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 700; }}
  .card-sub {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  .green  {{ color: var(--green); }}
  .red    {{ color: var(--red); }}
  .yellow {{ color: var(--yellow); }}
  .section-title {{ font-size: 13px; font-weight: 600; color: var(--muted);
                    text-transform: uppercase; letter-spacing: .05em; margin: 20px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card);
           border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  th {{ background: #21262d; color: var(--muted); font-size: 11px;
        text-transform: uppercase; padding: 8px 10px; text-align: left; }}
  td {{ padding: 7px 10px; border-top: 1px solid var(--border); font-size: 12px; }}
  .ticker {{ color: var(--muted); font-size: 11px; }}
  .badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px;
            font-size: 10px; font-weight: 700; }}
  .badge-win   {{ background: #1a3a1f; color: var(--green); }}
  .badge-loss  {{ background: #3a1a1a; color: var(--red); }}
  .badge-long  {{ background: #1a2a3a; color: var(--accent); }}
  .badge-short {{ background: #2a1a3a; color: #c084fc; }}
  .status-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  .dot-green  {{ background: var(--green); box-shadow: 0 0 6px var(--green); }}
  .dot-yellow {{ background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }}
  .dot-red    {{ background: var(--red);    box-shadow: 0 0 6px var(--red); }}
  .status-text {{ font-size: 12px; color: var(--muted); }}
  .paused-banner {{ background: #3a1a1a; border: 1px solid var(--red);
                    border-radius: 6px; padding: 10px 14px; margin-bottom: 14px;
                    color: var(--red); font-size: 13px; }}
  .bar-wrap {{ background: #21262d; border-radius: 4px; height: 8px;
               margin-top: 6px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; background: var(--green); transition: width .4s; }}
  .bar-fill.danger {{ background: var(--red); }}
  .chart-card {{ background: var(--card); border: 1px solid var(--border);
                 border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .chart-title {{ font-size: 12px; font-weight: 600; color: var(--muted);
                  text-transform: uppercase; letter-spacing: .05em; margin-bottom: 12px; }}
  .susp-table td {{ font-size: 12px; }}
  .susp-badge {{ display: inline-block; padding: 2px 7px; border-radius: 4px;
                 font-size: 10px; font-weight: 700; background: #3a2a1a; color: var(--yellow); }}
  .footer {{ margin-top: 20px; font-size: 11px; color: var(--muted); text-align: right; }}
</style>
</head>
<body>

<h1>⚡ Deriv Bot</h1>
<p class="subtitle">Auto-refreshes every 10 s &nbsp;|&nbsp; UTC {now_utc}</p>

{paused_banner}

<div class="status-row">
  <div class="dot {dot_class}"></div>
  <span class="status-text">
    <b>{session}</b> &nbsp;·&nbsp; Up {uptime}
    &nbsp;·&nbsp; Scanning: <b>{current_sym}</b>
    &nbsp;·&nbsp; Queue: <b>{queue_count}</b>
  </span>
</div>

<!-- ── Stat Cards ── -->
<div class="grid">

  <div class="card">
    <div class="card-title">Balance</div>
    <div class="card-value {balance_color}">${balance:.4f}</div>
    <div class="card-sub">Day start: ${day_start:.4f}</div>
  </div>

  <div class="card">
    <div class="card-title">Daily P&amp;L</div>
    <div class="card-value {pnl_color}">{pnl_sign}${abs(daily_pnl):.4f}</div>
    <div class="card-sub">{pnl_pct:+.2f}% &nbsp;·&nbsp; 15% loss limit</div>
    <div class="bar-wrap">
      <div class="bar-fill {danger_class}" style="width:{loss_bar:.0f}%"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Win Rate</div>
    <div class="card-value {wr_color}">{win_rate:.1f}%</div>
    <div class="card-sub">{wins}W / {losses}L / {trades} trades today</div>
  </div>

  <div class="card">
    <div class="card-title">Streak</div>
    <div class="card-value {streak_color}">{streak_disp}</div>
    <div class="card-sub">{streak_lbl}</div>
  </div>

  <div class="card">
    <div class="card-title">Session</div>
    <div class="card-value" style="font-size:15px;line-height:1.4">{session}</div>
    <div class="card-sub">Queue: {queue_count} symbols</div>
  </div>

  <div class="card">
    <div class="card-title">Last Signal</div>
    <div class="card-value" style="font-size:13px;line-height:1.4">{last_sig_sym}</div>
    <div class="card-sub">{last_sig_detail}</div>
  </div>

</div>

<!-- ── Balance Curve (inline SVG, no external deps) ── -->
<div class="chart-card">
  <div class="chart-title">Balance Curve — Today
    <span style="float:right;font-weight:400;color:#3fb950">● win</span>
    <span style="float:right;font-weight:400;color:#f85149;margin-right:10px">● loss</span>
  </div>
  {svg_chart}
</div>

<!-- ── Recent Trades ── -->
<div class="section-title">Recent Trades</div>
<table>
  <thead>
    <tr>
      <th>Time (UTC)</th><th>Symbol</th><th>Dir</th>
      <th>Stake</th><th>P&amp;L</th><th>Balance After</th><th>Result</th>
    </tr>
  </thead>
  <tbody>{trade_rows}</tbody>
</table>

<!-- ── Suspended Symbols ── -->
<div class="section-title">Suspended Symbols</div>
<table class="susp-table">
  <thead><tr><th>Symbol</th><th>Status</th></tr></thead>
  <tbody>{susp_rows}</tbody>
</table>

<!-- ── Symbol Leaderboard ── -->
<div class="section-title">Symbol Leaderboard</div>
<table>
  <thead>
    <tr><th>Symbol</th><th>Trades</th><th>Win %</th><th>P&amp;L</th><th>Score</th></tr>
  </thead>
  <tbody>{sym_rows}</tbody>
</table>

<div class="footer">Deriv Bot &middot; Render deployment</div>
</body>
</html>"""

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


# ── Flask routes ───────────────────────────────────────────────────────────────

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
    wins      = int(s.get("wins", s.get("wins_today", 0)))
    losses    = int(s.get("losses", s.get("losses_today", 0)))
    trades    = int(s.get("total_trades", wins + losses))
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
        "streak_label":      str(s.get("streak_label", "—")),
        "paused":            bool(s.get("paused_for_loss_limit", False)),
        "current_symbol":    str(s.get("current_symbol", "—")),
        "session":           str(s.get("session", "—")),
        "uptime_seconds":    int(s.get("uptime_seconds", 0)),
        "last_signal":       s.get("last_signal", "—"),
        "tradeable_count":   int(s.get("tradeable_count", 0)),
        "active_trades":     int(s.get("active_trades", 0)),
        "suspended_symbols": s.get("suspended_symbols", []),
    })


@app.route("/trades")
def trades_route():
    return jsonify({"recent_trades": _state.get("recent_trades", [])})


@app.route("/symbols")
def symbols_route():
    return jsonify({"symbols": _state.get("best_symbols", [])})


@app.route("/debug")
def debug_route():
    """Dump raw _state so we can see exactly what keys the bot engine writes."""
    import json
    safe = {}
    for k, v in _state.items():
        if isinstance(v, list):
            safe[k] = {
                "type": "list",
                "length": len(v),
                "first_item": v[0] if v else None,
            }
        elif isinstance(v, dict):
            safe[k] = {"type": "dict", "keys": list(v.keys())}
        else:
            safe[k] = v
    return jsonify(safe), 200


# ── Keep-alive ping loop ───────────────────────────────────────────────────────

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
    logger.info(
        f"Keep-alive pinger started "
        f"(interval={config.KEEP_ALIVE_INTERVAL}s, url={config.SELF_URL})"
    )
