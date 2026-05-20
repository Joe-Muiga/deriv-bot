"""
bot_engine.py – Central async orchestrator.

v11 → v12 changes (SCAN/RANK/EXECUTE rewrite per spec):

  SCAN LOOP:
    • Canonical cycle structure:
        while bot_running:
            cycle_start = time.time()
            [parallel scan all active non-suspended symbols via asyncio.gather]
            [collect + rank signals]
            [execute top N in parallel via asyncio.gather]
            [await settle]
            [update streaks, suspensions, stats]
            [check redeploy trigger]
            sleep(SCAN_CYCLE_SLEEP) — measured from cycle END
    • active_symbols = symbol_manager.get_queue() called fresh every cycle.
    • Dead-zone / suspension filtering handled inside get_queue() only.

  SIGNAL COLLECTION & RANKING:
    • 3/3 strength → unconditionally collected; no secondary gate.
    • 2/3 → collected only if signal_engine emitted it (direction != NONE).
    • Score = (strength/3 × 0.80) + (confidence/7 × 0.15) + (win_rate × 0.05)
      where win_rate = symbol_manager.win_rate(symbol) (0.0–1.0).
    • Sorted descending by score.
    • Deduplicated: one signal per symbol per cycle — highest score kept.
    • Candidate list capped at MAX_CONCURRENT_TRADES × 3.

  EXECUTION:
    • Top N = risk_manager.current_concurrent_limit() taken from ranked list.
    • All N launched with asyncio.gather(*[_execute_signal(s) for s in top_n]).
    • Each _execute_signal returns contract_id str or None on failure.
    • None result → log failure, try next ranked candidate same cycle (fallback
      loop after gather resolves).
    • executed_count += 1 only when contract_id is a non-None valid string.
    • Per-cycle log: CYCLE {n}: {scanned} scanned | {signals} signals |
                     {executing} executing | balance=${balance:.2f}

  SETTLE WAIT & PRE-SCAN:
    • After execute: await asyncio.sleep(TRADE_DURATION * 60 + 5)
    • During wait: prescan_task() runs as background coroutine, fills
      _prescan_buffer with pre-collected signals for next cycle.

  RESULT HANDLING:
    • All results fetched in parallel after settle wait.
    • Each passed to risk_manager.record_result() and
      symbol_manager.record_result().
    • Log: RESULT: {sym} {dir} → WIN/LOSS | P&L: ${pnl:+.2f} | Balance: ${bal:.2f}

  REDEPLOY AFTER N CYCLES:
    • _cycle_count increments after every completed settle wait.
    • At _cycle_count >= REDEPLOY_EVERY_N_CYCLES:
        – Stop accepting new signals.
        – Drain all open contracts (await their callbacks to fire).
        – Call trigger_redeploy().
        – await asyncio.sleep(300).
        – Reset _cycle_count = 0.
    • Log: REDEPLOY TRIGGERED: draining {n} open contracts before restart

  DAILY LOSS PROTECTION:
    • session_start_balance captured at bot launch.
    • After every result: if (session_start − current) / session_start
      >= DAILY_LOSS_LIMIT_PCT → halt, log DAILY LOSS LIMIT HIT — bot halted.

  SUSPENDED SYMBOL LOG (every cycle start):
    SUSPENDED: [{sym}({cycles_remaining}), ...] | ACTIVE: {n} symbols

  All v11 logic preserved unchanged:
    Strategy, SMC, signal_engine, candlestick_builder, news_filter,
    trade_journal, dashboard, websocket flow, balance callback, symbol init,
    _scan(), _execute(), _on_contract_result(), _on_tick().
"""

import asyncio
import datetime as _dt
import logging
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional

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

# Local constants (timing/dashboard only; SCAN_CYCLE_SLEEP is now in config)
DASHBOARD_PUSH_EVERY = 10
SYMBOL_REFRESH_EVERY = 3600
INIT_BATCH_SIZE      = 10


# ─── Result container ─────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol:      str
    sig:         SignalResult
    price:       float
    smc_ctx:     SMCContext
    ltf_atr:     float
    htf_atr:     float
    prob_score:  float = 0.0   # composite score (filled after _scan)


# ─── Bot engine ───────────────────────────────────────────────────────────────

class BotEngine:

    def __init__(self):
        self.client  = DerivClient()
        self.risk    = RiskManager(
            daily_loss_limit = config.DAILY_LOSS_LIMIT_PCT,
            risk_per_trade   = config.RISK_PER_TRADE_PCT,
            min_stake        = config.MIN_STAKE,
            max_stake        = config.MAX_STAKE,
            max_concurrent   = config.MAX_CONCURRENT_TRADES,
        )
        self.smc     = SMCAnalyzer(ob_expiry_bars=config.OB_EXPIRY_BARS)
        self.signal  = SignalEngine(
            min_modules = config.MIN_MODULES_FOR_SIGNAL,
            min_votes   = config.MIN_INDICATOR_VOTES,
        )
        self.news    = NewsFilter(block_minutes=config.NEWS_BLOCK_MINUTES)
        self.journal = TradeJournal()
        self.symbols = SymbolManager()

        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}
        self._initializing: set = set()
        self._open_contracts: dict = {}
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])
        self._last_symbol_refresh = 0.0

        self._confirmed_daily_loss: float = 0.0
        self._day_start_balance_local: float = 0.0
        self._confirmed_paused: bool = False
        self._current_utc_day: int = -1

        # v12: cycle counter — incremented after every completed settle wait
        self._cycle_count: int = 0

        # v12: pre-scan buffer — filled during settle wait, seeded into next cycle
        self._prescan_buffer: List[ScanResult] = []

        # v12: session-start balance for daily loss protection
        self._session_start_balance: float = 0.0

    # ── Timeframe routing ─────────────────────────────────────────────────────

    @staticmethod
    def _ltf_gran(symbol: str) -> int:
        return (config.FOREX_LTF_GRANULARITY
                if symbol in sym_module.FOREX
                else config.OTHER_LTF_GRANULARITY)

    # ── Composite probability score (BUG 2 — 3-component, 0.0–1.0) ───────────

    def _prob_score(self, result: ScanResult) -> float:
        """
        3-component weighted score per v12 spec:
          strength   80%  →  (sig.strength / 3) × 0.80
          confidence 15%  →  (sig.confidence / 7) × 0.15
          win_rate    5%  →  symbol_manager.win_rate(symbol) × 0.05

        Score range: 0.0 – 1.0
        """
        sig      = result.sig
        conf     = getattr(sig, "confidence", 0)
        win_rate = (self.symbols.win_rate(result.symbol)
                    if hasattr(self.symbols, "win_rate") else 0.5)

        strength_component  = (sig.strength / 3.0) * 0.80
        indicator_component = (conf / 7.0) * 0.15
        win_rate_component  = float(win_rate) * 0.05

        return round(strength_component + indicator_component + win_rate_component, 4)

    # ── Confirmed loss limit check ────────────────────────────────────────────

    def _check_confirmed_loss_limit(self):
        today = _dt.datetime.utcnow().day
        if today != self._current_utc_day:
            self._confirmed_daily_loss = 0.0
            self._day_start_balance_local = self.client.balance
            self._current_utc_day = today
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
        self._session_start_balance   = self.client.balance   # v12: daily loss anchor
        self._current_utc_day = _dt.datetime.utcnow().day

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
            batch = uninit[i : i + INIT_BATCH_SIZE]
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

    # ── Main loop (v12 rewrite) ────────────────────────────────────────────────

    async def _main_loop(self):
        scan_sleep     = getattr(config, "SCAN_CYCLE_SLEEP", 1)
        redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)
        max_cands      = config.MAX_CONCURRENT_TRADES * 3
        bot_running    = True
        cycle_number   = 0

        while bot_running:
            cycle_start = time.time()

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
                # Drain: wait until all callbacks fire naturally
                while self._open_contracts:
                    logger.info(
                        f"Draining — {len(self._open_contracts)} contract(s) still open")
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

            # ── Cycle bookkeeping ─────────────────────────────────────────
            self.symbols.decrement_suspensions()
            self.symbols.reset_cycle_used()

            # ── Active symbol list — fresh every cycle from SymbolManager ─
            active_symbols: List[str] = self.symbols.get_queue(max_symbols=200)
            # Filter to initialised symbols only (data must be ready)
            ready = [s for s in active_symbols if s in self._htf]

            # Build suspension log
            all_queue       = self.symbols.get_queue(max_symbols=200)
            suspended_info  = []
            for s in all_queue:
                cycles_rem = getattr(self.symbols, "suspension_remaining", lambda x: 0)(s)
                if self.symbols.is_suspended(s):
                    suspended_info.append(f"{s}({cycles_rem})")
            logger.info(
                f"SUSPENDED: [{', '.join(suspended_info) if suspended_info else '-'}] "
                f"| ACTIVE: {len(ready)} symbols")

            if not ready:
                await asyncio.sleep(scan_sleep)
                continue

            cycle_number += 1
            scanned_count = len(ready)

            # ── Parallel scan ALL active symbols simultaneously ────────────
            raw_results = await asyncio.gather(
                *[self._scan(s) for s in ready],
                return_exceptions=True,
            )

            # ── Collect qualified signals ─────────────────────────────────
            # Seed with pre-scan buffer from previous cycle's settle wait
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

            # Sort descending; cap to MAX_CONCURRENT_TRADES × 3
            ranked: List[ScanResult] = sorted(
                best_per_symbol.values(),
                key=lambda r: r.prob_score,
                reverse=True,
            )[:max_cands]

            signals_count = len(ranked)
            concurrent_limit: int = self.risk.current_concurrent_limit()
            top_n = ranked[:concurrent_limit]

            logger.info(
                f"CYCLE {cycle_number}: {scanned_count} scanned | "
                f"{signals_count} signals | {len(top_n)} executing | "
                f"balance=${self.client.balance:.2f}")

            if not top_n:
                await asyncio.sleep(scan_sleep)
                continue

            # ── Execute top N in parallel ─────────────────────────────────
            exec_results = await asyncio.gather(
                *[self._execute_signal(r) for r in top_n],
                return_exceptions=True,
            )

            # Count successes; attempt fallback for failures within same cycle
            executed_count   = 0
            failed_slots     = 0
            executed_symbols: set = set()

            for i, (sig_r, res) in enumerate(zip(top_n, exec_results)):
                if isinstance(res, Exception):
                    logger.error(
                        f"_execute_signal({sig_r.symbol}) raised: {res}")
                    failed_slots += 1
                elif res is not None:
                    executed_count += 1
                    executed_symbols.add(sig_r.symbol)
                else:
                    logger.warning(
                        f"Placement failed for {sig_r.symbol} — "
                        f"trying next ranked candidate")
                    failed_slots += 1

            # Fallback: attempt next ranked candidates for failed slots
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
                    conf = getattr(fb.sig, "confidence", 0)
                    if not self.risk.can_trade(
                            signal_strength=int(fb.sig.strength),
                            confidence=int(conf)):
                        continue
                    try:
                        fb_cid = await self._execute_signal(fb)
                        if fb_cid is not None:
                            executed_count += 1
                            executed_symbols.add(fb.symbol)
                            remaining_slots -= 1
                        else:
                            logger.warning(
                                f"Fallback placement also failed: {fb.symbol}")
                    except Exception as exc:
                        logger.error(f"Fallback execute error ({fb.symbol}): {exc}")

            # ── Settle wait + background pre-scan ─────────────────────────
            if executed_count > 0:
                wait_secs = config.TRADE_DURATION * 60 + 5
                logger.info(
                    f"{executed_count} trade(s) placed — "
                    f"settling for {wait_secs}s")

                # Run prescan in background during settle wait
                prescan_task = asyncio.create_task(self._prescan_task(ready))
                await asyncio.sleep(wait_secs)
                await prescan_task   # ensure it finishes before next cycle

                # ── Result collection ─────────────────────────────────────
                # Results arrive via _on_contract_result callbacks.
                # risk_manager and symbol_manager already updated there.
                # Additional daily-loss check happens in that callback too.

                # ── Daily loss check (post-settle) ────────────────────────
                if self._session_start_balance > 0:
                    loss_ratio = (
                        (self._session_start_balance - self.client.balance)
                        / self._session_start_balance
                    )
                    if loss_ratio >= config.DAILY_LOSS_LIMIT_PCT:
                        logger.critical(
                            "DAILY LOSS LIMIT HIT — bot halted")
                        bot_running = False
                        break

                # Cycle complete — increment counter
                self._cycle_count += 1
            else:
                # No trades placed this cycle
                pass

            # ── Cycle sleep measured from cycle END ───────────────────────
            elapsed   = time.time() - cycle_start
            remainder = max(0.0, scan_sleep - elapsed)
            if remainder > 0:
                await asyncio.sleep(remainder)

    # ── execute_signal: wraps _execute, returns contract_id or None ───────────

    async def _execute_signal(self, sig_r: ScanResult) -> Optional[str]:
        """
        Thin wrapper around _execute().  Returns the contract_id string on
        success, None if the API rejects.  Never raises — all exceptions are
        caught and logged, returning None so the caller can substitute.
        """
        try:
            conf = getattr(sig_r.sig, "confidence", 0)
            if not self.risk.can_trade(
                    signal_strength=int(sig_r.sig.strength),
                    confidence=int(conf)):
                return None

            if self.symbols.is_used(sig_r.symbol):
                return None

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
                # Return a non-None sentinel — actual cid stored in _open_contracts
                # We return "ok" so caller knows it succeeded; _execute already
                # stores the real cid internally.
                return "ok"
            return None

        except Exception as exc:
            logger.error(f"_execute_signal({sig_r.symbol}): {exc}")
            return None

    # ── Pre-scan task: fills _prescan_buffer during settle wait ───────────────

    async def _prescan_task(self, symbols: List[str]):
        """
        Runs in background during the settle wait.  Scans all symbols and
        stores qualified signals into _prescan_buffer to seed the next cycle.
        """
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

            # Phase A – HTF SMC
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

            # Phase B.1 – Price in SMC zone?
            ltf_bars_list = ltf.completed_bars
            if not ltf_bars_list:
                return None

            current_price = float(ltf_bars_list[-1].close)

            in_zone = self.smc.price_in_smc_zone(current_price, smc_ctx.bias, smc_ctx)
            if not in_zone:
                return None

            # Phase B.2–B.3 – Modules
            sig = self.signal.evaluate(
                ltf_bars = ltf_bars_list,
                htf_bias = smc_ctx.bias,
                smc_ctx  = smc_ctx,
                in_zone  = in_zone,
                symbol   = symbol,
            )
            if sig.direction == "NONE":
                return None

            # News filter
            if self.news.is_blocked(symbol):
                return None

            # Volatility filter
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
        """
        Place a trade.  Returns True on successful placement, False if the API
        rejects (buy_resp is None).  Raises on unexpected errors so the caller
        can log them and substitute the next ranked signal.
        """
        stake = self.risk.calculate_stake()
        ac    = get_symbol_class(symbol)

        conf    = getattr(sig, "confidence", "?")
        fresh   = getattr(smc_ctx, "zone_freshness", "?")

        # Direction follows sig.direction directly — NEVER inverted
        direction = sig.direction   # "LONG" or "SHORT"

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
            m1             = sig.m1_signal,
            m2             = sig.m2_signal,
            m3             = sig.m3_signal,
            modules        = sig.strength,
        )

        self._open_contracts[cid] = (symbol, direction, stake, price, rec)
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
        if not info:
            return
        symbol, direction, stake, entry_price, rec = info

        won = pnl > 0

        # risk_manager
        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)
        if hasattr(self.risk, "record_result"):
            try:
                self.risk.record_result(symbol=symbol, won=won, pnl=pnl)
            except Exception:
                pass

        # journal
        self.journal.close_trade(
            contract_id   = cid,
            exit_price    = sell_price,
            pnl           = pnl,
            payout        = payout,
            balance_after = bal_after,
        )

        # symbol_manager — both legacy record_trade and spec record_result
        self.symbols.record_trade(symbol, won=won, pnl=pnl)
        if hasattr(self.symbols, "record_result"):
            try:
                self.symbols.record_result(symbol=symbol, won=won, pnl=pnl)
            except Exception:
                pass

        if pnl < 0:
            self._confirmed_daily_loss += abs(pnl)

        self._check_confirmed_loss_limit()
        set_active_trades(len(self._open_contracts))

        # ── Spec-required RESULT log ──────────────────────────────────────
        logger.info(
            f"RESULT: {symbol} {direction} → "
            f"{'WIN' if won else 'LOSS'} | "
            f"P&L: ${pnl:+.2f} | Balance: ${bal_after:.2f}")

        # ── Daily loss protection (session-level) ─────────────────────────
        if self._session_start_balance > 0:
            loss_ratio = (
                (self._session_start_balance - bal_after)
                / self._session_start_balance
            )
            if loss_ratio >= config.DAILY_LOSS_LIMIT_PCT:
                logger.critical(
                    "DAILY LOSS LIMIT HIT — bot halted")
                # Signal the main loop to stop
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
