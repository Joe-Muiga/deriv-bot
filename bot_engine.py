"""
bot_engine.py – Central async orchestrator.

v13 — Maximum concurrent aggressive execution rewrite per spec.

KEY CHANGES vs v12
──────────────────
CONTINUOUS SCANNING:
  • Scanning and settling run independently.
  • After executing trades, scanning continues immediately — do NOT block
    on settlement.  Settlement is handled by a background asyncio.Task.
  • Scan loop runs every SCAN_CYCLE_SLEEP seconds regardless of open contracts.
  • Only gate on execution: do not place new trades if concurrent slots full.

EXECUTION FLOW (every cycle):
  • Scan ALL initialised symbols in parallel via asyncio.gather.
  • Collect qualified signals, deduplicate, rank by composite score.
  • Filter: suspended / in-gap / already-active symbols removed.
  • Execute top N = current_concurrent_limit in parallel.
  • Immediately start next scan — no settle wait blocking the scan loop.
  • Settlement callbacks fire asynchronously and free slots naturally.

CYCLE LOG:
  CYCLE X | Scanned: N | Signals: M | Executing: K | Open: J | Streak: +S | Balance: $B

PRESERVED (unchanged):
  Symbol suspension timing, orphan contract handling, dead zone logic,
  websocket connection, dashboard serving, Render redeploy mechanism,
  daily loss protection, pre-scan buffer, redeploy cycle budget.

v12 logic preserved:
  SCAN/RANK score formula, prescan buffer, orphan resolution,
  stale contract blocking, daily loss limit, redeploy trigger,
  _on_contract_result, _on_tick, _init_data, _execute, _scan,
  contract-closure guarantee.
"""

import asyncio
import datetime as _dt
import logging
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import numpy as np

import config
import restart_scheduler
import symbols as sym_module
from deriv_client import DerivClient
from candlestick_builder import CandlestickBuilder
from smc_analyzer import SMCAnalyzer, SMCContext
from signal_engine import SignalEngine, SignalResult
from risk_manager import RiskManager
from news_filter import NewsFilter
from trade_journal import TradeJournal
from symbol_manager import SymbolManager
import indicators as ind
from keep_alive import update_status, set_active_trades, is_redeploy_pending
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

DASHBOARD_PUSH_EVERY = 10
SYMBOL_REFRESH_EVERY = 3600
INIT_BATCH_SIZE      = 10

_ORPHAN_MAX_ATTEMPTS = 3


# ─── Result container ─────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol:      str
    sig:         SignalResult
    price:       float
    smc_ctx:     SMCContext
    ltf_atr:     float
    htf_atr:     float
    prob_score:  float = 0.0


# ─── Bot engine ───────────────────────────────────────────────────────────────

class BotEngine:

    def __init__(self):
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])
        self._cycle_count: int = 0
        self._running: bool = False
        self._prescan_buffer: List[ScanResult] = []
        self._session_start_balance: float = 0.0
        self._open_contracts: dict = {}
        self._contract_open_times: dict = {}
        self._active_symbols: Set[str] = set()

        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}
        self._initializing: set = set()
        self._last_symbol_refresh = 0.0

        self._confirmed_daily_loss: float = 0.0
        self._day_start_balance_local: float = 0.0
        self._confirmed_paused: bool = False
        self._current_utc_day: int = -1

        # Background settle tasks: set of asyncio.Task
        self._settle_tasks: Set[asyncio.Task] = set()

        self.client  = DerivClient()
        self.risk    = RiskManager(
            risk_per_trade   = config.RISK_PER_TRADE_PCT,
            min_stake        = config.MIN_STAKE,
            max_stake        = config.MAX_STAKE,
            max_concurrent   = config.MAX_CONCURRENT_TRADES,
        )
        self.smc     = SMCAnalyzer(ob_expiry_bars=config.OB_EXPIRY_BARS)
        self.signal  = SignalEngine(
            symbols=self._queue,
            config=config,
        )
        self.news    = NewsFilter(block_minutes=config.NEWS_BLOCK_MINUTES)
        self.journal = TradeJournal()
        self.symbols = SymbolManager()

    # ── Timeframe routing ─────────────────────────────────────────────────────

    @staticmethod
    def _ltf_gran(symbol: str) -> int:
        return (config.FOREX_LTF_GRANULARITY
                if symbol in sym_module.FOREX
                else config.OTHER_LTF_GRANULARITY)

    # ── Composite probability score ───────────────────────────────────────────

    def _prob_score(self, result: ScanResult) -> float:
        """
        3-component weighted score:
          strength   80%  →  (sig.strength / 3) × 0.80
          confidence 15%  →  (sig.confidence / 7) × 0.15
          win_rate    5%  →  symbol_manager.win_rate(symbol) × 0.05
        """
        sig      = result.sig
        conf     = getattr(sig, "confidence", 0)
        win_rate = (self.symbols.win_rate(result.symbol)
                    if hasattr(self.symbols, "win_rate") else 0.5)

        return round(
            (sig.strength / 3.0) * 0.80
            + (conf / 7.0) * 0.15
            + float(win_rate) * 0.05,
            4,
        )

    # ── Confirmed loss limit check ────────────────────────────────────────────

    def _check_confirmed_loss_limit(self):
        today = _dt.datetime.utcnow().day
        if today != self._current_utc_day:
            self._confirmed_daily_loss    = 0.0
            self._day_start_balance_local = self.client.balance
            self._current_utc_day         = today
            logger.info(
                f"UTC day reset — day_start_balance=${self._day_start_balance_local:.4f}")

        if self._day_start_balance_local > 0:
            loss_ratio = self._confirmed_daily_loss / self._day_start_balance_local
            self._confirmed_paused = loss_ratio >= config.DAILY_LOSS_LIMIT_PCT
        else:
            self._confirmed_paused = False

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Trading Bot  –  parallel-scan / streak-stake")
        logger.info("=" * 64)

        ws_task = asyncio.create_task(self.client.connect())
        for _ in range(60):
            if self.client.is_connected:
                break
            await asyncio.sleep(1)

        if not self.client.is_connected:
            logger.error("Could not connect/authorise within 60 s")
            ws_task.cancel()
            return

        self.client.on_balance(self._on_balance)
        self.risk.set_balance(self.client.balance)

        self._day_start_balance_local = self.client.balance
        self._session_start_balance   = self.client.balance
        self._current_utc_day         = _dt.datetime.utcnow().day

        await self._refresh_symbols()
        await self._init_all_symbols()

        try:
            self._push_dashboard()
        except Exception:
            pass

        dash_task = asyncio.create_task(self._dashboard_loop())
        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.critical(f"Main loop crashed: {exc}\n{traceback.format_exc()}")
        finally:
            dash_task.cancel()
            ws_task.cancel()
            # Cancel any pending settle tasks
            for t in list(self._settle_tasks):
                t.cancel()

    # ── Balance callback ──────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)

    # ── Symbol discovery ──────────────────────────────────────────────────────

    async def _refresh_symbols(self):
        active_raw = await self.client.get_active_symbols()
        synth_pfx  = ("R_", "1HZ", "BOOM", "CRASH", "JD",
                       "RDBEAR", "RDBULL", "stpRNG")
        active_syms = [
            s["symbol"] for s in active_raw
            if s.get("exchange_is_open", 0) == 1
            or any(s["symbol"].startswith(p) for p in synth_pfx)
        ]
        self.symbols.update_active(active_syms)
        self._queue = self.symbols.get_queue(max_symbols=200)
        self._last_symbol_refresh = time.time()
        logger.info(f"Symbol queue: {len(self._queue)} instruments")

    # ── Bulk init ─────────────────────────────────────────────────────────────

    async def _init_all_symbols(self):
        uninit = [s for s in self._queue if s not in self._htf]
        logger.info(f"Initialising {len(uninit)} symbols …")
        for i in range(0, len(uninit), INIT_BATCH_SIZE):
            batch = uninit[i: i + INIT_BATCH_SIZE]
            await asyncio.gather(*[self._init_data(s) for s in batch],
                                 return_exceptions=True)
        logger.info(f"{len(self._htf)} symbols ready")

    # ── Dashboard ─────────────────────────────────────────────────────────────

    async def _dashboard_loop(self):
        while True:
            try:
                self._push_dashboard()
            except Exception:
                pass
            await asyncio.sleep(DASHBOARD_PUSH_EVERY)

    def _push_dashboard(self):
        summary = self.journal.session_summary()
        risk_s  = self.risk.summary()
        update_status(
            running               = True,
            balance               = self.client.balance,
            day_start_balance     = self.risk.day_start_balance,
            paused_for_loss_limit = self._confirmed_paused,
            trades_today          = risk_s["total_trades"],
            wins_today            = risk_s["wins"],
            losses_today          = risk_s["losses"],
            session               = self.symbols.current_session,
            tradeable_count       = len(self._queue),
            gross_profit          = summary.get("gross_profit", 0),
            gross_loss            = summary.get("gross_loss", 0),
            profit_factor         = summary.get("profit_factor", 0),
            avg_rr                = summary.get("avg_rr", 0),
            best_trade            = summary.get("best_trade", 0),
            worst_trade           = summary.get("worst_trade", 0),
            streak                = risk_s.get("streak", 0),
            recent_trades         = self.journal.recent_trades(20),
            best_symbols          = self.symbols.best_symbols(10),
        )

    # ── Post-settle orphan resolution (unchanged from v12) ────────────────────

    async def _resolve_remaining_contracts(self):
        remaining = list(self._open_contracts.keys())
        n   = len(remaining)
        now = time.time()

        if n == 0:
            logger.info("Open contracts remaining: 0")
            return

        age_parts = []
        for cid in remaining:
            age_secs = int(now - self._contract_open_times.get(cid, now))
            age_parts.append(f"{cid}({age_secs}s)")
        logger.info(f"Open contracts remaining: {n} — " + ", ".join(age_parts))

        for cid in remaining:
            if cid not in self._open_contracts:
                continue

            resolved = False
            for attempt in range(1, _ORPHAN_MAX_ATTEMPTS + 1):
                try:
                    resp = await self.client.force_check_contract(cid)
                    poc  = resp.get("proposal_open_contract", {})
                    is_closed = bool(
                        poc.get("is_sold")
                        or poc.get("is_expired")
                        or poc.get("status") in ("sold", "won", "lost")
                    )
                    if is_closed:
                        logger.info(
                            f"force_check_contract({cid}): confirmed closed "
                            f"(attempt {attempt}) — firing _on_contract_result")
                        await self._on_contract_result(cid, resp)
                        resolved = True
                        break
                    else:
                        logger.debug(
                            f"force_check_contract({cid}): still open "
                            f"(attempt {attempt}/{_ORPHAN_MAX_ATTEMPTS})")
                        if attempt < _ORPHAN_MAX_ATTEMPTS:
                            await asyncio.sleep(5)
                except Exception as exc:
                    logger.warning(f"force_check_contract({cid}) attempt {attempt} error: {exc}")
                    if attempt < _ORPHAN_MAX_ATTEMPTS:
                        await asyncio.sleep(5)

            if not resolved and cid in self._open_contracts:
                info       = self._open_contracts.get(cid, ())
                symbol     = info[0] if len(info) > 0 else "UNKNOWN"
                stake      = info[2] if len(info) > 2 else 0.0
                rec        = info[4] if len(info) > 4 else None

                logger.error(f"ORPHANED: {cid} forcing close")
                self.journal.close_trade(
                    contract_id   = cid,
                    exit_price    = 0,
                    pnl           = -stake,
                    payout        = 0,
                    balance_after = self.client.balance,
                )
                self._confirmed_daily_loss += stake
                self._check_confirmed_loss_limit()
                try:
                    self.symbols.record_result(symbol, won=False)
                except Exception:
                    pass
                if rec is not None:
                    try:
                        self.risk.register_close(rec, exit_price=0, pnl=-stake)
                    except Exception:
                        pass

                self._open_contracts.pop(cid, None)
                self._contract_open_times.pop(cid, None)
                set_active_trades(len(self._open_contracts))
                try:
                    self._push_dashboard()
                except Exception:
                    pass
                logger.error(
                    f"ORPHAN RECORDED AS LOSS: {cid} {symbol} "
                    f"-${stake:.2f} | balance=${self.client.balance:.4f}")

    def _has_stale_contracts(self) -> bool:
        stale_threshold = config.TRADE_DURATION * 60 + 30
        now = time.time()
        for cid, open_time in list(self._contract_open_times.items()):
            if cid in self._open_contracts:
                if now - open_time > stale_threshold:
                    return True
        return False

    # ── Background settle handler ─────────────────────────────────────────────

    async def _settle_and_resolve(self, wait_secs: float, ready: List[str]):
        """
        Runs as a background asyncio.Task.
        Waits for settlement, resolves orphans, increments cycle count.
        Does NOT block the main scan loop.
        """
        try:
            # Pre-scan during settle wait
            prescan_task = asyncio.create_task(self._prescan_task(ready))
            await asyncio.sleep(wait_secs)
            await prescan_task

            await self._resolve_remaining_contracts()

            # Daily loss check (session-level)
            if self._session_start_balance > 0:
                loss_ratio = (
                    (self._session_start_balance - self.client.balance)
                    / self._session_start_balance
                )
                if loss_ratio >= config.DAILY_LOSS_LIMIT_PCT:
                    logger.critical("DAILY LOSS LIMIT HIT — bot halted")
                    self._confirmed_paused = True

            # Increment redeploy counter
            self._cycle_count += 1

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"_settle_and_resolve error: {exc}")

    # ── Main loop (v13 — continuous scanning) ────────────────────────────────

    async def _main_loop(self):
        scan_sleep     = getattr(config, "SCAN_CYCLE_SLEEP", 1)
        redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)
        max_cands      = config.MAX_CONCURRENT_TRADES * 3
        bot_running    = True
        cycle_number   = 0

        while bot_running:
            cycle_start = time.time()

            # ── Decrement any active loss-streak pause ────────────────────
            self.risk.decrement_pause()

            # ── Symbol cache refresh ──────────────────────────────────────
            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()
                await self._init_all_symbols()

            # ── keep_alive redeploy flag ──────────────────────────────────
            if is_redeploy_pending():
                if self._open_contracts:
                    logger.info(
                        f"Redeploy pending – waiting for "
                        f"{len(self._open_contracts)} open trade(s)")
                    await asyncio.sleep(10)
                    continue
                else:
                    logger.info(
                        "Redeploy pending – all trades closed, safe to restart")
                    await asyncio.sleep(30)
                    continue

            # ── Cycle-count–based redeploy ────────────────────────────────
            if self._cycle_count >= redeploy_every:
                n_open = len(self._open_contracts)
                logger.info(
                    f"REDEPLOY TRIGGERED: draining {n_open} open contracts before restart")
                while self._open_contracts:
                    logger.info(f"Draining — {len(self._open_contracts)} contract(s) still open")
                    await asyncio.sleep(5)
                restart_scheduler.trigger_redeploy()
                logger.info("Redeploy triggered — standing by for 300 s")
                await asyncio.sleep(300)
                self._cycle_count = 0
                continue

            # ── Confirmed drawdown pause ──────────────────────────────────
            if self._confirmed_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(
                    f"⛔ Confirmed drawdown limit – paused, "
                    f"resumes in ~{mins:.0f} min")
                await asyncio.sleep(60)
                continue

            # ── Loss-streak cycle pause ───────────────────────────────────
            if self.risk.pause_cycles_remaining > 0:
                logger.info(
                    f"⏸ Loss-streak pause: skipping cycle "
                    f"({self.risk.pause_cycles_remaining} remaining) | "
                    f"streak={self.risk.current_streak}")
                self.risk.consume_pause_cycle()
                await asyncio.sleep(scan_sleep)
                continue

            # ── Block cycle only if stale contracts exist ─────────────────
            if self._has_stale_contracts():
                stale_cids = [
                    cid for cid in self._open_contracts
                    if (time.time() - self._contract_open_times.get(cid, time.time()))
                    > config.TRADE_DURATION * 60 + 30
                ]
                logger.warning(
                    f"Blocking new cycle — stale contracts: {stale_cids} — running force-check")
                await self._resolve_remaining_contracts()
                await asyncio.sleep(scan_sleep)
                continue

            # ── Cycle bookkeeping ─────────────────────────────────────────
            self.symbols.decrement_suspensions()
            self.symbols.reset_cycle_used()

            # ── Active symbol list — fresh every cycle ────────────────────
            active_symbols: List[str] = self.symbols.get_queue(max_symbols=200)
            ready = [s for s in active_symbols if s in self._htf]

            # Suspension log
            all_queue      = self.symbols.get_queue(max_symbols=200)
            suspended_info = []
            for s in all_queue:
                if self.symbols.is_suspended(s):
                    cycles_rem = self.symbols.get_suspension_remaining(s)
                    suspended_info.append(f"{s}({cycles_rem})")
            logger.info(
                f"SUSPENDED: [{', '.join(suspended_info) if suspended_info else '-'}] "
                f"| ACTIVE: {len(ready)} symbols")

            if not ready:
                await asyncio.sleep(scan_sleep)
                continue

            cycle_number += 1
            scanned_count = len(ready)

            # ── Parallel scan ALL active symbols ──────────────────────────
            raw_results = await asyncio.gather(
                *[self._scan(s) for s in ready],
                return_exceptions=True,
            )

            # ── Collect qualified signals (seed from prescan buffer) ──────
            all_candidates: List[ScanResult] = list(self._prescan_buffer)
            self._prescan_buffer = []

            for r in raw_results:
                if not isinstance(r, ScanResult):
                    continue
                if r.sig.direction == "NONE":
                    continue
                r.prob_score = self._prob_score(r)
                all_candidates.append(r)

            # ── Deduplicate: best score per symbol ────────────────────────
            best_per_symbol: Dict[str, ScanResult] = {}
            for r in all_candidates:
                if (r.symbol not in best_per_symbol
                        or r.prob_score > best_per_symbol[r.symbol].prob_score):
                    best_per_symbol[r.symbol] = r

            ranked: List[ScanResult] = sorted(
                best_per_symbol.values(),
                key=lambda r: r.prob_score,
                reverse=True,
            )[:max_cands]

            signals_count    = len(ranked)
            concurrent_limit = self.risk.current_concurrent_limit

            # ── Triple gate: filter suspended / in-gap / already-active ───
            ranked = [
                r for r in ranked
                if not self.symbols.is_suspended(r.symbol)
                and self.symbols.can_trade_now(r.symbol)
                and r.symbol not in self._active_symbols
            ]

            top_n = ranked[:concurrent_limit]

            open_count = len(self._open_contracts)
            logger.info(
                f"CYCLE {cycle_number} | "
                f"Scanned: {scanned_count} | "
                f"Signals: {signals_count} | "
                f"Executing: {len(top_n)} | "
                f"Open: {open_count} | "
                f"Streak: {'+' if self.risk.current_streak >= 0 else ''}{self.risk.current_streak} | "
                f"Balance: ${self.client.balance:.2f}")

            if not top_n:
                # Nothing to execute this cycle — keep scanning
                elapsed   = time.time() - cycle_start
                remainder = max(0.0, scan_sleep - elapsed)
                if remainder > 0:
                    await asyncio.sleep(remainder)
                continue

            # ── Execute top N in parallel ─────────────────────────────────
            exec_results = await asyncio.gather(
                *[self._execute_signal(r) for r in top_n],
                return_exceptions=True,
            )

            executed_count   = 0
            failed_slots     = 0
            executed_symbols: Set[str] = set()

            for i, (sig_r, res) in enumerate(zip(top_n, exec_results)):
                if isinstance(res, Exception):
                    logger.error(f"_execute_signal({sig_r.symbol}) raised: {res}")
                    failed_slots += 1
                elif res is not None:
                    executed_count += 1
                    executed_symbols.add(sig_r.symbol)
                else:
                    logger.warning(
                        f"Placement failed for {sig_r.symbol} — trying next ranked candidate")
                    failed_slots += 1

            # Fallback for failed slots
            if failed_slots > 0:
                fallback_candidates = [
                    r for r in ranked[concurrent_limit:]
                    if r.symbol not in executed_symbols
                ]
                remaining_slots = failed_slots
                for fb in fallback_candidates:
                    if remaining_slots <= 0:
                        break
                    if fb.symbol in executed_symbols:
                        continue
                    if not self.risk.can_trade():
                        continue
                    try:
                        fb_cid = await self._execute_signal(fb)
                        if fb_cid is not None:
                            executed_count += 1
                            executed_symbols.add(fb.symbol)
                            remaining_slots -= 1
                        else:
                            logger.warning(f"Fallback placement also failed: {fb.symbol}")
                    except Exception as exc:
                        logger.error(f"Fallback execute error ({fb.symbol}): {exc}")

            # ── Launch background settle task — DO NOT block scan loop ────
            if executed_count > 0:
                wait_secs = config.TRADE_DURATION * 60 + 5
                logger.info(
                    f"{executed_count} trade(s) placed — "
                    f"settle task launched ({wait_secs}s), scanning continues")

                settle_task = asyncio.create_task(
                    self._settle_and_resolve(wait_secs, list(ready))
                )
                self._settle_tasks.add(settle_task)
                settle_task.add_done_callback(self._settle_tasks.discard)

            # ── Cycle sleep measured from cycle END — always runs ─────────
            elapsed   = time.time() - cycle_start
            remainder = max(0.0, scan_sleep - elapsed)
            if remainder > 0:
                await asyncio.sleep(remainder)

    # ── execute_signal ────────────────────────────────────────────────────────

    async def _execute_signal(self, sig_r: ScanResult) -> Optional[str]:
        try:
            if not self.risk.can_trade():
                return None
            if self.symbols.is_used(sig_r.symbol):
                return None

            conf = getattr(sig_r.sig, "confidence", 0)
            logger.info(
                f"Executing: {sig_r.symbol} {sig_r.sig.direction} "
                f"score={sig_r.prob_score:.4f} "
                f"strength={sig_r.sig.strength}/3 confidence={conf}/7")
            update_status(
                current_symbol=sig_r.symbol,
                last_signal=(
                    f"[{sig_r.symbol}] {sig_r.sig.direction} | {sig_r.sig.reason}"
                ),
            )

            success = await self._execute(
                sig_r.symbol, sig_r.sig, sig_r.price, sig_r.smc_ctx)

            if success:
                self.symbols.mark_used(sig_r.symbol)
                return "ok"
            return None

        except Exception as exc:
            logger.error(f"_execute_signal({sig_r.symbol}): {exc}")
            return None

    # ── Pre-scan task ─────────────────────────────────────────────────────────

    async def _prescan_task(self, symbols: List[str]):
        try:
            raw = await asyncio.gather(
                *[self._scan(s) for s in symbols],
                return_exceptions=True,
            )
            buffer: List[ScanResult] = []
            for r in raw:
                if not isinstance(r, ScanResult):
                    continue
                if r.sig.direction == "NONE":
                    continue
                r.prob_score = self._prob_score(r)
                buffer.append(r)
            self._prescan_buffer = buffer
            logger.debug(f"Pre-scan complete: {len(buffer)} signal(s) buffered")
        except Exception as exc:
            logger.debug(f"_prescan_task: {exc}")
            self._prescan_buffer = []

    # ── Per-symbol scan ───────────────────────────────────────────────────────

    async def _scan(self, symbol: str) -> Optional[ScanResult]:
        try:
            htf = self._htf.get(symbol)
            ltf = self._ltf.get(symbol)
            if htf is None or ltf is None:
                return None
            if htf.count < 20 or ltf.count < 30:
                return None

            htf_bars    = htf.completed_bars
            H = np.array([b.high  for b in htf_bars])
            L = np.array([b.low   for b in htf_bars])
            C = np.array([b.close for b in htf_bars])
            htf_atr_arr = ind.atr(H, L, C, 14)
            valid_ha    = htf_atr_arr[~np.isnan(htf_atr_arr)]
            htf_atr     = float(valid_ha[-1]) if len(valid_ha) else 0.0

            smc_ctx = self.smc.analyse(htf_bars, htf_atr, symbol=symbol)
            if smc_ctx.bias == "NEUTRAL":
                return None

            ltf_bars_list = ltf.completed_bars
            if not ltf_bars_list:
                return None

            current_price = float(ltf_bars_list[-1].close)
            in_zone = self.smc.price_in_smc_zone(current_price, smc_ctx.bias, smc_ctx)
            if not in_zone:
                return None

            sig = self.signal.evaluate(
                ltf_bars = ltf_bars_list,
                htf_bias = smc_ctx.bias,
                smc_ctx  = smc_ctx,
                in_zone  = in_zone,
                symbol   = symbol,
            )
            if sig.direction == "NONE":
                return None

            if self.news.is_blocked(symbol):
                return None

            ltf_H = np.array([b.high  for b in ltf_bars_list])
            ltf_L = np.array([b.low   for b in ltf_bars_list])
            ltf_C = np.array([b.close for b in ltf_bars_list])
            ltf_atr_arr = ind.atr(ltf_H, ltf_L, ltf_C, 14)
            valid_la    = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
            ltf_atr     = float(valid_la[-1]) if len(valid_la) else 0.0

            if htf_atr > 0 and ltf_atr > 2 * htf_atr:
                return None

            return ScanResult(
                symbol  = symbol,
                sig     = sig,
                price   = current_price,
                smc_ctx = smc_ctx,
                ltf_atr = ltf_atr,
                htf_atr = htf_atr,
            )

        except Exception as exc:
            logger.debug(f"_scan({symbol}): {exc}")
            return None

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult,
                       price: float, smc_ctx: SMCContext) -> bool:
        if self.symbols.is_suspended(symbol):
            logger.info(f"EXECUTION BLOCKED: {symbol} is suspended — skipping")
            return False
        if not self.symbols.can_trade_now(symbol):
            logger.info(f"EXECUTION BLOCKED: {symbol} within minimum gap — skipping")
            return False
        if symbol in self._active_symbols:
            logger.info(f"EXECUTION BLOCKED: {symbol} already has open contract — skipping")
            return False

        stake     = await self.risk.calculate_stake()
        ac        = get_symbol_class(symbol)
        conf      = getattr(sig, "confidence", "?")
        fresh     = getattr(smc_ctx, "zone_freshness", "?")
        direction = sig.direction

        logger.info(
            f"▶ {direction} {symbol} | ${stake:.2f} | "
            f"struct={smc_ctx.structure} | modules={sig.strength}/3 | "
            f"confidence={conf}/7 | "
            f"freshness={fresh if isinstance(fresh, str) else f'{fresh:.2f}'} | "
            f"streak={self.risk.current_streak}")

        buy_resp = await self.client.buy_contract(
            symbol    = symbol,
            direction = direction,
            stake     = stake,
            duration  = config.TRADE_DURATION,
            dur_unit  = config.TRADE_DURATION_UNIT,
        )
        if not buy_resp:
            logger.warning(f"Order rejected by API for {symbol} — slot will be substituted")
            return False

        cid   = str(buy_resp.get("contract_id", ""))
        bal_b = self.client.balance

        rec = self.risk.register_open(
            symbol=symbol, direction=direction,
            stake=stake, entry_price=price)

        self.journal.open_trade(
            contract_id    = cid,
            symbol         = symbol,
            direction      = direction,
            stake          = stake,
            entry_price    = price,
            balance_before = bal_b,
            asset_class    = ac,
            htf_bias       = smc_ctx.bias,
            smc_structure  = smc_ctx.structure,
            m1             = getattr(sig, "m1_signal", 0),
            m2             = getattr(sig, "m2_signal", 0),
            m3             = getattr(sig, "m3_signal", sig.strength),
            modules        = sig.strength,
        )

        self._open_contracts[cid]       = (symbol, direction, stake, price, rec)
        self._contract_open_times[cid]  = time.time()
        self.symbols.record_trade_placed(symbol)
        self._active_symbols.add(symbol)
        set_active_trades(len(self._open_contracts))

        await self.client.subscribe_contract(
            cid,
            lambda msg, _cid=cid: asyncio.create_task(
                self._on_contract_result(_cid, msg)))

        return True

    # ── Contract result callback ──────────────────────────────────────────────

    async def _on_contract_result(self, cid: str, msg: dict):
        poc = msg.get("proposal_open_contract", {})
        if not poc.get("is_sold"):
            return

        sell_price = float(poc.get("sell_price", 0))
        pnl        = float(poc.get("profit",     0))
        payout     = float(poc.get("payout",     sell_price))
        bal_after  = self.client.balance

        info = self._open_contracts.pop(cid, None)
        self._contract_open_times.pop(cid, None)
        if not info:
            return
        symbol, direction, stake, entry_price, rec = info

        won = pnl > 0

        # Free the symbol slot immediately — next scan can pick it up
        self._active_symbols.discard(symbol)

        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)

        self.journal.close_trade(
            contract_id   = cid,
            exit_price    = sell_price,
            pnl           = pnl,
            payout        = payout,
            balance_after = bal_after,
        )

        self.symbols.record_result(symbol=symbol, won=won)

        if pnl < 0:
            self._confirmed_daily_loss += abs(pnl)

        self._check_confirmed_loss_limit()
        set_active_trades(len(self._open_contracts))

        logger.info(
            f"RESULT: {symbol} {direction} → "
            f"{'WIN' if won else 'LOSS'} | "
            f"P&L: ${pnl:+.2f} | Balance: ${bal_after:.2f}")

        # Session-level daily loss protection
        if self._session_start_balance > 0:
            loss_ratio = (
                (self._session_start_balance - bal_after)
                / self._session_start_balance
            )
            if loss_ratio >= config.DAILY_LOSS_LIMIT_PCT:
                logger.critical("DAILY LOSS LIMIT HIT — bot halted")
                self._confirmed_paused = True

        try:
            self._push_dashboard()
        except Exception:
            pass

    # ── Data initialisation ───────────────────────────────────────────────────

    async def _init_data(self, symbol: str):
        if symbol in self._initializing or symbol in self._htf:
            return
        self._initializing.add(symbol)
        try:
            ltf_gran = self._ltf_gran(symbol)

            htf_b = CandlestickBuilder(granularity=config.HTF_GRANULARITY,
                                       max_bars=config.HTF_BARS + 20)
            ltf_b = CandlestickBuilder(granularity=ltf_gran,
                                       max_bars=config.LTF_BARS + 20)

            htf_data, ltf_data = await asyncio.gather(
                self.client.get_candles(symbol, config.HTF_GRANULARITY, config.HTF_BARS),
                self.client.get_candles(symbol, ltf_gran, config.LTF_BARS),
                return_exceptions=True,
            )
            if isinstance(htf_data, Exception): htf_data = []
            if isinstance(ltf_data, Exception): ltf_data = []

            if not htf_data and not ltf_data:
                logger.warning(f"No data for {symbol} – skipping")
                return

            if htf_data: htf_b.seed(htf_data)
            if ltf_data: ltf_b.seed(ltf_data)

            self._htf[symbol] = htf_b
            self._ltf[symbol] = ltf_b

            await self.client.subscribe_ticks(
                symbol,
                lambda tick, s=symbol: self._on_tick(s, tick))

            logger.info(f"{symbol}: ready | htf={htf_b.count} | "
                        f"ltf={ltf_b.count} (ltf_gran={ltf_gran}s)")

        except Exception as exc:
            logger.error(f"_init_data({symbol}): {exc}")
        finally:
            self._initializing.discard(symbol)

    def _on_tick(self, symbol: str, tick: dict):
        import time as _t
        epoch = int(tick.get("epoch", int(_t.time())))
        price = float(tick.get("quote", 0))
        if price == 0:
            return
        if symbol in self._ltf:
            self._ltf[symbol].add_tick(epoch, price)
        if symbol in self._htf:
            self._htf[symbol].add_tick(epoch, price)
