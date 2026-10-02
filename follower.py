"""
follower.py — the Follower (Oct 2026, Scout+Follower phase).

Copies the Scout's entries ONLY while the edge gate is open, with its own
virtual balance, tier stakes, profit pause, Donkey guard, exposure ceiling and
per-hour cap. NOT a second BotEngine: a light component with its own
DerivClient (own token) and its own instance state.

MODES (config.FOLLOWER_MODE)
  shadow  places nothing. Logs what it WOULD trade and settles the mirrored
          trade from the Scout's own CONFIRMED result (same contract), scaled
          to the Follower's stake. No token needed.
  demo    places on the Follower demo token; virtual $10,000 balance updated
          ONLY from confirmed settlements of its own trades.
  live    real token, REAL balance read from the account. Refuses to start
          unless config.FOLLOWER_LIVE_CONFIRM == LIVE_CONFIRM_PHRASE.

BASELINE: for every Scout entry (gate open or not) it also tracks what
"always follow" would have made with the same tier-stake rule, so gated vs
always-follow PnL can be compared.

Only CONFIRMED settled results are counted anywhere (is_confirmed_close).
"""
import asyncio
import json
import logging
import os
import time
from typing import Callable, Dict, Optional

import config
import balance_tiers
from donkey_guard import DonkeyGuard
from profit_pause import ProfitPause

logger = logging.getLogger("follower")

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"
MODES = ("shadow", "demo", "live")


def _confirmed(poc) -> bool:
    try:
        from deriv_client import is_confirmed_close
        return is_confirmed_close(poc)
    except Exception:                      # offline tests without aiohttp
        if not poc:
            return False
        if poc.get("is_sold"):
            return True
        return str(poc.get("status", "")).lower() in ("won", "lost", "sold")


def _cfg(name, default):
    return getattr(config, name, default)


class Follower:
    def __init__(self, gate, mode: Optional[str] = None,
                 client_factory: Optional[Callable] = None,
                 writer: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.time,
                 start_balance: Optional[float] = None):
        mode = (mode or _cfg("FOLLOWER_MODE", "shadow")).strip().lower()
        if mode not in MODES:
            raise ValueError(f"FOLLOWER_MODE must be one of {MODES}, got {mode!r}")
        if mode == "live" and _cfg("FOLLOWER_LIVE_CONFIRM", "") != LIVE_CONFIRM_PHRASE:
            raise ValueError(
                "FOLLOWER live mode refused: FOLLOWER_LIVE_CONFIRM is not set to "
                "the confirmation phrase. No real-money trading in this phase.")
        self.mode = mode
        self.gate = gate
        self._clock = clock
        self._factory = client_factory
        self._write = writer or (lambda ev: None)
        self.client = None
        self.ml = None           # online_learner.OnlineLearner (set by the hub)
        self.tuner = None        # auto_tuner.AutoTuner (set by the hub)

        self.balance = float(start_balance if start_balance is not None
                             else _cfg("FOLLOWER_START_BALANCE", 10000.0))
        self.baseline_balance = self.balance
        self.pause = ProfitPause()
        self.pause.on_boot(self.balance)
        self.guard = DonkeyGuard(
            edge_log_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "follower_edge_log.csv"),
            state_env_key="SF_FOLLOWER_GUARD",
            cap_override=int(_cfg("FOLLOWER_MAX_TRADES_PER_HOUR", 60)),
            label="FOLLOWER GUARD")

        self._open: Dict[str, dict] = {}       # cid -> {stake, ...} placed/shadow
        self._inflight_stake = 0.0
        self._seen_entries: set = set()
        self._seen_results: set = set()
        # Scout entries awaiting the Scout's confirmed result:
        # cid -> {gate_open, base_stake, shadow_stake}
        self._scout_pending: Dict[str, dict] = {}

        self.c = {   # cumulative counters (persisted)
            "taken": 0, "wins": 0, "losses": 0, "pnl": 0.0,
            "skipped_gate": 0, "skipped_guard": 0, "skipped_pause": 0,
            "skipped_limits": 0, "skipped_stale": 0, "skipped_unsupported": 0,
            "skipped_ml": 0, "skipped_noedge": 0,
            "place_failed": 0,
            "base_trades": 0, "base_wins": 0, "base_pnl": 0.0,
            "gate_on_n": 0, "gate_on_w": 0, "gate_off_n": 0, "gate_off_w": 0,
        }

    # ── helpers ─────────────────────────────────────────────────────────
    def stake(self) -> float:
        return float(balance_tiers.stake_for_balance(self.balance))

    def open_count(self) -> int:
        return len(self._open) + (1 if self._inflight_stake > 0 else 0)

    def open_stake(self) -> float:
        return sum(v["stake"] for v in self._open.values()) + self._inflight_stake

    def busy(self) -> bool:
        return self.open_count() > 0

    # ── Scout entry event ───────────────────────────────────────────────
    async def on_entry(self, ev: dict) -> None:
        cid = str(ev.get("contract_id", "") or "")
        if cid and cid in self._seen_entries:
            return
        if cid:
            self._seen_entries.add(cid)
            if len(self._seen_entries) > 500:
                self._seen_entries.clear()
        now = self._clock()
        gate_open = bool(self.gate.is_open())
        base_stake = self.stake_for(self.baseline_balance)
        rec = {"gate_open": gate_open, "base_stake": base_stake, "shadow_stake": 0.0}
        if cid:
            self._scout_pending[cid] = rec
            if len(self._scout_pending) > 400:
                self._scout_pending.pop(next(iter(self._scout_pending)))

        if not gate_open:
            self.c["skipped_gate"] += 1
            return
        try:
            import fixed_cycle
            if fixed_cycle.is_cooldown_requested():
                self.c["skipped_pause"] += 1
                return
        except Exception:
            pass
        if str(ev.get("kind", "")) not in ("DIGIT", "RISE_FALL"):
            self.c["skipped_unsupported"] += 1
            return
        age = now - float(ev.get("ts", now) or now)
        if age > float(_cfg("FOLLOWER_MAX_ENTRY_AGE_SECS", 8)):
            self.c["skipped_stale"] += 1
            return
        if self.pause.paused(now):
            self.c["skipped_pause"] += 1
            return
        self.pause.on_boot(self.balance)    # opens a fresh session after a pause
        ok, why = self.guard.can_enter(now)
        if not ok:
            self.c["skipped_guard"] += 1
            logger.info(f"FOLLOWER: guard blocks entry — {why}")
            return
        # Edge brake: the auto-tuner's honest verdict says the gated follower is
        # losing on the newest data -> stand aside until that clears.
        if (getattr(self.tuner, "no_edge", False) and bool(_cfg("REGIME_EDGE_BRAKE", True))):
            self.c["skipped_noedge"] += 1
            return
        # ML veto (only once trained AND proven better than baseline out-of-sample)
        if self.ml is not None:
            ok_ml, why_ml = self.ml.allow(cid)
            if not ok_ml:
                self.c["skipped_ml"] += 1
                logger.info(f"FOLLOWER: ML veto — {why_ml}")
                return
        stake = self.stake()
        max_open = int(_cfg("FOLLOWER_MAX_OPEN", 3))
        # small-account fix: 10% of a $2 balance is $0.21 — below the $0.35 minimum
        # stake, which blocked EVERY entry. Always allow at least one stake open.
        ceiling = max(float(_cfg("FOLLOWER_MAX_EXPOSURE_PCT", 0.10)) * self.balance, stake)
        if (self.open_count() >= max_open
                or self.open_stake() + stake > ceiling
                or stake > float(_cfg("MAX_STAKE", 2000.0))):
            self.c["skipped_limits"] += 1
            logger.info(f"FOLLOWER: exposure/open limit blocks entry "
                        f"(open={self.open_count()}, stake=${stake:g}, ceiling=${ceiling:,.0f})")
            return

        if self.mode == "shadow":
            rec["shadow_stake"] = stake
            self.guard.note_entry(now)
            self.c["taken"] += 1
            logger.info(f"FOLLOWER[shadow]: WOULD TRADE {ev.get('symbol')} "
                        f"{ev.get('contract_type')} barrier={ev.get('barrier')} "
                        f"stake=${stake:g} (balance ${self.balance:,.2f})")
            return
        await self._place(ev, stake, rec)

    def stake_for(self, balance: float) -> float:
        return float(balance_tiers.stake_for_balance(balance))

    async def _place(self, ev: dict, stake: float, rec: dict) -> None:
        if self.client is None:
            self.c["place_failed"] += 1
            return
        self._inflight_stake += stake
        try:
            if ev["kind"] == "DIGIT":
                resp = await self.client.buy_digit_contract(
                    symbol=ev["symbol"], stake=stake, digit=int(ev["barrier"]),
                    match_type=ev["contract_type"])
            else:
                resp = await self.client.buy_contract(
                    symbol=ev["symbol"], direction=ev["direction"], stake=stake,
                    multiplier=None)
        except Exception as exc:
            logger.error(f"FOLLOWER: placement error: {exc}")
            resp = None
        finally:
            self._inflight_stake = max(0.0, self._inflight_stake - stake)
        if not resp:
            self.c["place_failed"] += 1
            return
        fcid = str(resp.get("contract_id", "") or "")
        if not fcid:
            self.c["place_failed"] += 1
            return
        self.guard.note_entry(self._clock())
        self.c["taken"] += 1
        self._open[fcid] = {"stake": stake, "symbol": ev["symbol"],
                            "contract_type": ev.get("contract_type"),
                            "barrier": ev.get("barrier"), "strategy": ev.get("strategy"),
                            "opened_at": self._clock()}
        logger.info(f"FOLLOWER[{self.mode}]: OPENED {fcid} {ev['symbol']} "
                    f"{ev.get('contract_type')} barrier={ev.get('barrier')} stake=${stake:g}")
        try:
            await self.client.subscribe_contract(
                fcid, lambda msg, _c=fcid: asyncio.create_task(self._on_poc(_c, msg)),
                symbol=ev["symbol"])
        except Exception as exc:
            logger.warning(f"FOLLOWER: subscribe_contract({fcid}): {exc}")

    # ── Follower's own settlement (demo / live) ─────────────────────────
    async def _on_poc(self, fcid: str, msg: dict) -> None:
        poc = msg.get("proposal_open_contract", msg)
        if not _confirmed(poc):
            return                       # never count an unconfirmed close
        info = self._open.pop(fcid, None)
        if info is None:
            return
        try:
            self.client.stop_tracking(fcid)
        except Exception:
            pass
        pnl = float(poc.get("profit", 0) or 0)
        payout = float(poc.get("payout", poc.get("sell_price", 0)) or 0)
        self._settle(fcid, info, pnl, payout, source="follower")

    def _settle(self, cid: str, info: dict, pnl: float, payout: float,
                source: str) -> None:
        key = f"{source}:{cid}"
        if key in self._seen_results:
            return
        self._seen_results.add(key)
        if len(self._seen_results) > 500:
            self._seen_results.clear()
        stake = float(info["stake"])
        won = pnl > 0
        if pnl != 0:                        # pnl == 0: neither win nor loss
            self.c["wins" if won else "losses"] += 1
        self.c["pnl"] += pnl
        if self.mode == "live" and self.client is not None:
            try:
                if self.client.balance > 0:
                    self.balance = float(self.client.balance)
            except Exception:
                self.balance += pnl
        else:
            self.balance += pnl
        self.guard.record(
            contract_id=f"{source}:{cid}", confirmed=True,
            symbol=info.get("symbol", ""), signal_kind=str(info.get("strategy", "")),
            contract=str(info.get("contract_type", "")), barrier=info.get("barrier", ""),
            stake=stake, payout=payout, won=won, pnl=pnl)
        self._write({"who": "follower", "mode": self.mode, "ts": int(self._clock()),
                     "contract_id": cid, "symbol": info.get("symbol"),
                     "contract_type": info.get("contract_type"),
                     "barrier": info.get("barrier"), "stake": stake,
                     "pnl": round(pnl, 4), "won": int(won),
                     "balance_after": round(self.balance, 2)})
        mins = self.pause.check(self.balance, self._clock())
        if mins:
            self.guard.reset_session()
            logger.warning(f"FOLLOWER: PROFIT TARGET hit — pausing new entries "
                           f"{mins:.1f} min (balance ${self.balance:,.2f})")
        logger.info(f"FOLLOWER {'✅ WIN' if won else '❌ LOSS' if pnl < 0 else '➖ FLAT'} "
                    f"pnl={pnl:+.2f} balance=${self.balance:,.2f}")

    # ── Scout confirmed result event (baseline, ON/OFF tallies, shadow) ──
    def on_scout_result(self, ev: dict) -> None:
        if not ev.get("confirmed", True):
            return                                  # unconfirmed: never counted
        cid = str(ev.get("contract_id", "") or "")
        rec = self._scout_pending.pop(cid, None)
        if rec is None:
            return
        try:
            pnl = float(ev["pnl"]); sstake = float(ev["stake"])
        except Exception:
            return
        if sstake <= 0 or pnl == 0 or not ev.get("gate_eligible", True):
            return
        ratio = pnl / sstake                        # P&L per unit stake
        won = pnl > 0
        # gate ON vs OFF quality (state recorded at ENTRY time)
        k = "gate_on" if rec["gate_open"] else "gate_off"
        self.c[k + "_n"] += 1
        self.c[k + "_w"] += int(won)
        # baseline: always follow, same tier-stake rule
        bs = rec["base_stake"]
        self.c["base_trades"] += 1
        self.c["base_wins"] += int(won)
        bp = ratio * bs
        self.c["base_pnl"] += bp
        self.baseline_balance += bp
        # shadow: mirrored trade settles off the Scout's confirmed result
        if self.mode == "shadow" and rec["shadow_stake"] > 0:
            st = rec["shadow_stake"]
            self._settle(cid, {"stake": st, "symbol": ev.get("symbol"),
                               "contract_type": ev.get("contract_type"),
                               "barrier": ev.get("barrier"),
                               "strategy": ev.get("strategy")},
                         ratio * st, float(ev.get("payout", 0) or 0), source="shadow")

    # ── restart recovery of contracts still open on Deriv ───────────────
    async def recover_open(self) -> None:
        if self.client is None:
            return
        for fcid in list(self._open):
            try:
                poc = await self.client.force_check_contract(fcid)
                if _confirmed(poc):
                    await self._on_poc(fcid, {"proposal_open_contract": poc})
                else:
                    await self.client.subscribe_contract(
                        fcid, lambda msg, _c=fcid: asyncio.create_task(self._on_poc(_c, msg)))
            except Exception as exc:
                logger.warning(f"FOLLOWER: recover {fcid}: {exc}")

    # ── persistence ─────────────────────────────────────────────────────
    def export(self) -> dict:
        return {"balance": round(self.balance, 4),
                "baseline_balance": round(self.baseline_balance, 4),
                "c": {k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in self.c.items()},
                "pause": self.pause.export(),
                "guard": self.guard.export_state(),
                "open": self._open}

    def load(self, d: dict) -> None:
        try:
            self.balance = float(d.get("balance", self.balance))
            self.baseline_balance = float(d.get("baseline_balance", self.baseline_balance))
            for k, v in (d.get("c") or {}).items():
                if k in self.c:
                    self.c[k] = type(self.c[k])(v)
            self.pause.load(d.get("pause") or {})
            if self.pause.start_balance is None and not self.pause.paused(self._clock()):
                self.pause.on_boot(self.balance)
            g = d.get("guard")
            if g:
                os.environ["SF_FOLLOWER_GUARD"] = g
                self.guard.load_from_env()
            self._open = {str(k): v for k, v in (d.get("open") or {}).items()}
            logger.info(f"FOLLOWER: restored (balance ${self.balance:,.2f}, "
                        f"{len(self._open)} open)")
        except Exception as exc:
            logger.warning(f"FOLLOWER: could not restore state: {exc}")

    def state(self) -> dict:
        now = self._clock()
        c = self.c
        n = c["wins"] + c["losses"]
        return {"mode": self.mode, "balance": round(self.balance, 2),
                "stake": self.stake(), "open": self.open_count(),
                "taken": c["taken"], "wins": c["wins"], "losses": c["losses"],
                "win_rate": (c["wins"] / n) if n else None,
                "pnl": round(c["pnl"], 2),
                "paused_secs": round(self.pause.seconds_left(now), 0),
                "guard": self.guard.can_enter(now)[1] or "ok",
                "baseline_pnl": round(c["base_pnl"], 2),
                "baseline_trades": c["base_trades"],
                "baseline_balance": round(self.baseline_balance, 2),
                "gate_on_wr": (c["gate_on_w"] / c["gate_on_n"]) if c["gate_on_n"] else None,
                "gate_on_n": c["gate_on_n"],
                "gate_off_wr": (c["gate_off_w"] / c["gate_off_n"]) if c["gate_off_n"] else None,
                "gate_off_n": c["gate_off_n"],
                "skipped": {k[8:]: v for k, v in c.items() if k.startswith("skipped_")},
                "place_failed": c["place_failed"]}
