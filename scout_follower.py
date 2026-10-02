"""
scout_follower.py — wiring for the Scout + Follower phase (Oct 2026).

  Scout   = the existing BotEngine, on its own demo token. After every
            CONFIRMED settlement it publishes a "result" event; when it opens a
            contract it publishes an "entry" event (see publish_* below).
  Gate    = edge_gate.EdgeGate fed by the Scout's results.
  Follower= follower.Follower, copies Scout entries while the gate is open.
  Hub     = this module: owns the three, persistence, reporting.

All of it is inert unless config.SCOUT_FOLLOWER_ENABLED is True.

PERSISTENCE: Render env vars via fixed_cycle._persist_env_vars, written ONCE
per leg, in the same batch as the cooldown deadline (fixed_cycle.
enter_cooldown_now -> export_env()). That batch is immediately followed by the
redeploy anyway, so no extra writes. A hard crash mid-leg loses at most that
leg's evidence. Keys: SF_GATE_STATE, SF_FOLLOWER_STATE, SF_SCOUT_STATS.
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional

import config
from edge_gate import EdgeGate
from event_bus import bus

logger = logging.getLogger("scout_follower")
_HERE = os.path.dirname(os.path.abspath(__file__))


def enabled() -> bool:
    return bool(getattr(config, "SCOUT_FOLLOWER_ENABLED", False))


def scout_data_mode() -> bool:
    return enabled() and bool(getattr(config, "SCOUT_DATA_MODE", True))


class Hub:
    def __init__(self, follower_factory=None, clock=time.time, events_path=None):
        self._clock = clock
        self.gate = EdgeGate(clock=clock)
        self.follower = None
        self.follower_error = ""
        self._events_path = events_path or os.path.join(
            _HERE, getattr(config, "SF_EVENTS_FILE", "sf_events.jsonl"))
        self._tasks = []
        self._client = None
        self.scout_buckets: dict = {}        # hour_epoch -> [n, wins, pnl, sumR]
        self.scout_total = {"n": 0, "wins": 0, "pnl": 0.0}
        self.started = clock()
        self._load_env()
        try:
            from follower import Follower
            self.follower = (follower_factory or Follower)(self.gate, clock=clock,
                                                          writer=self.write_event)
        except Exception as exc:
            self.follower_error = str(exc)
            logger.critical(f"FOLLOWER NOT STARTED: {exc}")
        if self.follower is not None:
            self._load_follower_env()
        bus.clear()
        bus.subscribe("result", self._on_scout_result)
        if self.follower is not None:
            bus.subscribe("entry", self.follower.on_entry)
            bus.subscribe("result", self.follower.on_scout_result)
        logger.warning(
            f"SCOUT+FOLLOWER enabled | follower mode="
            f"{self.follower.mode if self.follower else 'DISABLED'} | gate window="
            f"{self.gate.window} min_trades={self.gate.min_trades} "
            f"open P>={self.gate.open_prob} close P<{self.gate.close_prob}")

    # ── events ──────────────────────────────────────────────────────────
    def write_event(self, ev: dict) -> None:
        """Append-only settled-event file + the same row in the log stream
        (Render's disk is wiped on redeploy; logs are the durable copy)."""
        try:
            line = json.dumps(ev, separators=(",", ":"), default=str)
            logger.info("SF_EVENT," + line)
            with open(self._events_path, "a") as fh:
                fh.write(line + "\n")
        except Exception as exc:
            logger.warning(f"SF: event write failed: {exc}")

    def _on_scout_result(self, ev: dict) -> None:
        if not ev.get("confirmed", True):
            return
        self.gate.on_result(ev)
        pnl = float(ev.get("pnl", 0) or 0)
        stake = float(ev.get("stake", 0) or 0)
        if pnl != 0:
            hr = str(int(self._clock() // 3600))
            b = self.scout_buckets.setdefault(hr, [0, 0, 0.0, 0.0])
            b[0] += 1; b[1] += int(pnl > 0); b[2] += pnl
            b[3] += (pnl / stake) if stake > 0 else 0.0
            for k in sorted(self.scout_buckets)[:-26]:
                del self.scout_buckets[k]
            self.scout_total["n"] += 1
            self.scout_total["wins"] += int(pnl > 0)
            self.scout_total["pnl"] += pnl
        self.write_event({"who": "scout", **{k: ev.get(k) for k in (
            "ts", "contract_id", "symbol", "strategy", "contract_type", "barrier",
            "duration", "stake", "payout", "pnl", "won")}})

    # ── persistence ─────────────────────────────────────────────────────
    def export_env(self) -> dict:
        out = {"SF_GATE_STATE": json.dumps(self.gate.export(), separators=(",", ":")),
               "SF_SCOUT_STATS": json.dumps({"b": self.scout_buckets,
                                             "t": self.scout_total}, separators=(",", ":"))}
        if self.follower is not None:
            out["SF_FOLLOWER_STATE"] = json.dumps(self.follower.export(),
                                                  separators=(",", ":"))
        return out

    def _load_env(self) -> None:
        for key, fn in (("SF_GATE_STATE", self.gate.load), ("SF_SCOUT_STATS", self._load_stats)):
            raw = os.environ.get(key, "")
            if raw:
                try:
                    fn(json.loads(raw))
                except Exception as exc:
                    logger.warning(f"SF: could not restore {key}: {exc}")

    def _load_stats(self, d: dict) -> None:
        self.scout_buckets = {str(k): v for k, v in (d.get("b") or {}).items()}
        self.scout_total.update(d.get("t") or {})

    def _load_follower_env(self) -> None:
        raw = os.environ.get("SF_FOLLOWER_STATE", "")
        if raw:
            try:
                self.follower.load(json.loads(raw))
            except Exception as exc:
                logger.warning(f"SF: could not restore follower: {exc}")

    # ── lifecycle (runs inside the Scout's asyncio loop) ────────────────
    async def start(self) -> None:
        f = self.follower
        if f is not None and f.mode in ("demo", "live"):
            token = os.environ.get("DERIV_FOLLOWER_TOKEN", "")
            if not token:
                self.follower_error = "DERIV_FOLLOWER_TOKEN not set"
                logger.critical("FOLLOWER: DERIV_FOLLOWER_TOKEN not set — follower disabled "
                                "(Scout keeps running)")
                bus._subs["entry"].clear()
                self.follower = None
                f = None
            else:
                from deriv_client import DerivClient
                f.client = DerivClient(
                    token=token, app_id=config.DERIV_APP_ID,
                    account_mode="real" if f.mode == "live" else "demo")
                self._client = f.client
                self._tasks.append(asyncio.create_task(f.client.connect()))
                for _ in range(60):
                    if f.client.is_connected:
                        break
                    await asyncio.sleep(1)
                if not f.client.is_connected:
                    logger.critical("FOLLOWER: could not connect within 60s — placing "
                                    "nothing this leg")
                    f.client = None
                else:
                    await asyncio.sleep(1)
                    if f.mode == "live" and f.client.balance > 0:
                        f.balance = float(f.client.balance)
                    await f.recover_open()
        self._tasks.append(asyncio.create_task(self._report_loop()))

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    def busy(self) -> bool:
        return bool(self.follower and self.follower.busy())

    # ── reporting ───────────────────────────────────────────────────────
    def _window_stats(self, hours: int) -> dict:
        cur = int(self._clock() // 3600)
        n = w = 0; pnl = 0.0
        for k, b in self.scout_buckets.items():
            if cur - int(k) < hours:
                n += b[0]; w += b[1]; pnl += b[2]
        return {"trades": n, "wins": w, "losses": n - w, "pnl": round(pnl, 2)}

    def summary(self) -> dict:
        return {"ts": int(self._clock()), "gate": self.gate.state(),
                "scout_1h": self._window_stats(1), "scout_24h": self._window_stats(24),
                "scout_total": self.scout_total,
                "follower": self.follower.state() if self.follower else
                            {"disabled": self.follower_error}}

    def report_lines(self) -> list:
        s = self.summary(); g = s["gate"]; f = s["follower"]
        out = [
            f"SF REPORT | GATE {'OPEN' if g['open'] else 'CLOSED'} {g['held_secs']/60:.1f}min "
            f"({g['reason']}) | wr {g['win_rate']*100:.1f}% of {g['n']} vs breakeven "
            f"{g['breakeven']*100:.2f}% | P={g['prob']:.2f} | on/off cycles {g['cycles']}",
            f"SF REPORT | SCOUT 1h {s['scout_1h']['wins']}W/{s['scout_1h']['losses']}L "
            f"pnl {s['scout_1h']['pnl']:+.2f} | 24h {s['scout_24h']['wins']}W/"
            f"{s['scout_24h']['losses']}L pnl {s['scout_24h']['pnl']:+.2f}"]
        if "disabled" in f:
            out.append(f"SF REPORT | FOLLOWER disabled: {f['disabled']}")
        else:
            on = f"{f['gate_on_wr']*100:.1f}%" if f["gate_on_wr"] is not None else "n/a"
            off = f"{f['gate_off_wr']*100:.1f}%" if f["gate_off_wr"] is not None else "n/a"
            out.append(
                f"SF REPORT | FOLLOWER[{f['mode']}] bal ${f['balance']:,.2f} stake "
                f"${f['stake']:g} taken {f['taken']} ({f['wins']}W/{f['losses']}L) pnl "
                f"{f['pnl']:+,.2f} | pause {f['paused_secs']:.0f}s | guard {f['guard']}")
            out.append(
                f"SF REPORT | GATED pnl {f['pnl']:+,.2f} vs ALWAYS-FOLLOW "
                f"{f['baseline_pnl']:+,.2f} ({f['baseline_trades']} trades) | scout wr "
                f"gate-ON {on} (n={f['gate_on_n']}) vs gate-OFF {off} (n={f['gate_off_n']})")
        return out

    async def _report_loop(self) -> None:
        every = float(getattr(config, "SF_REPORT_EVERY_SECS", 120))
        while True:
            try:
                await asyncio.sleep(every)
                self.report()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(f"SF: report failed: {exc}")

    def report(self) -> None:
        for line in self.report_lines():
            logger.warning(line)
        try:
            with open(os.path.join(_HERE, getattr(config, "SF_SUMMARY_FILE",
                                                   "sf_summary.json")), "w") as fh:
                json.dump(self.summary(), fh, default=str)
        except Exception:
            pass


# ── module-level singleton + helpers used by bot_engine / fixed_cycle ───────
_hub: Optional[Hub] = None


def get_hub() -> Optional[Hub]:
    global _hub
    if not enabled():
        return None
    if _hub is None:
        _hub = Hub()
    return _hub


def follower_busy() -> bool:
    return bool(_hub and _hub.busy())


def export_env() -> dict:
    """Called from fixed_cycle.enter_cooldown_now(); {} when disabled."""
    return _hub.export_env() if _hub else {}


def publish_entry(**ev) -> None:
    if _hub is not None:
        ev.setdefault("ts", time.time())
        bus.publish("entry", ev)


def publish_result(**ev) -> None:
    if _hub is not None:
        ev.setdefault("ts", time.time())
        bus.publish("result", ev)
