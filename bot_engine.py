"""
bot_engine.py – Central async orchestrator.

v11 → v12 changes:

  REMOVED — Loss-streak cycle pause:
    The `if self.risk.pause_cycles_remaining > 0` block and all related
    risk.consume_pause_cycle() calls have been removed.  Global pausing on
    loss streak no longer exists.

  NEW — Per-symbol cycle suspension (integrated with SymbolManager v4):
    At the TOP of every _main_loop iteration:
      1. self.symbols.decrement_suspensions() is called to tick down counters.
      2. Currently suspended symbols are logged to Render logs.
      3. The `ready` list (symbols to scan this cycle) excludes any symbol
         for which self.symbols.is_suspended(symbol) is True.

    On every confirmed trade close (_on_contract_result):
      • LOSS → self.symbols.suspend(symbol, cycles=config.SYMBOL_SUSPENSION_CYCLES)
      • WIN  → self.symbols.clear_suspension(symbol)

    All other symbols continue scanning and trading normally while any
    individual symbol is suspended.

  REMOVED — risk._streak_tier() reference from ranked-signal log line
    (tier no longer exists in RiskManager v9).

  All v9/v10 composite score formula, ranked signal logging, deduplication
  rule, confidence gate passing, SMC/signal/module logic, contract placement,
  and scan frequency unchanged.

v12 → v13 changes:

  CHANGE 1 — Failed trade substitution:
    _execute() now returns bool (True = contract placed, False = placement
    failed for any reason).  The Round-1 and Round-2 execution loops track
    attempted_symbols separately from executed_symbols.  A None return from
    buy_contract() is treated as a hard failure — the symbol is added to
    attempted_symbols only, executed_count is NOT incremented, and iteration
    continues to the next ranked signal as a substitute.  If the ranked list
    is exhausted with no substitute, the slot is left empty and the bot
    continues without idling.  Every failure and every substitution is logged.

  CHANGE 2 — Automatic Render redeploy every 5 cycles:
    _cycle_count (int, starts 0) is incremented at the end of every
    completed settle-wait (i.e. every cycle in which at least one trade
    was placed).  When _cycle_count reaches config.REDEPLOY_EVERY_N_CYCLES:
      • set_redeploy_pending(True) is called to stop new trades.
      • The engine drains _open_contracts to zero (polling every 10 s).
      • keep_alive.trigger_redeploy() fires the Render Deploy Hook.
      • asyncio.sleep(300) then _main_loop returns; the process idles until
        Render kills and restarts it with the new deploy.

  SMC logic, signal ranking, confidence gates, zone freshness, symbol
  suspension, win-streak stake scaling, module logic, scan frequency, and
  all other v12 behaviour are UNCHANGED.
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
from keep_alive import (update_status, set_active_trades,
                        is_redeploy_pending, set_redeploy_pending)
import keep_alive as _keep_alive_mod
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

SCAN_CYCLE_SLEEP     = 1
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

        # ── v13: cycle counter + redeploy state ───────────────────────────────
        self._cycle_count: int = 0
        self._redeploy_triggered: bool = False

    # ── Timeframe routing ─────────────────────────────────────────────────────

    @staticmethod
    def _ltf_gran(symbol: str) -> int:
        return (config.FOREX_LTF_GRANULARITY
                if symbol in sym_module.FOREX
                else config.OTHER_LTF_GRANULARITY)

    # ── Composite probability score ───────────────────────────────────────────

    @staticmethod
    def _prob_score(result: ScanResult) -> float:
        """
        Three-component weighted score:
          Module strength (0–3)   × weight 40%  → contribution 0–1.6
          Confidence (0–7 indics) × weight 35%  → contribution 0–1.4
          Zone freshness (0–1)    × weight 25%  → contribution 0–1.0
        Total max ≈ 4.0

        confidence comes from sig.confidence (added in signal_engine v9).
        zone_freshness comes from smc_ctx.zone_freshness (added in smc_analyzer v7).
        """
        strength_component   = (result.sig.strength / 3.0) * 4.0 * 0.40
        confidence_component = (getattr(result.sig, "confidence", 0) / 7.0) * 4.0 * 0.35
        freshness_component  = getattr(result.smc_ctx, "zone_freshness", 0.5) * 4.0 * 0.25
        return round(strength_component + confidence_component + freshness_component, 4)

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

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _main_loop(self):
        while True:

            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()
                await self._init_all_symbols()

            # ── External redeploy-pending guard (from restart_scheduler) ──────
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

            if self._confirmed_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(
                    f"⛔ 90% confirmed drawdown – paused, resumes in ~{mins:.0f} min")
                await asyncio.sleep(60)
                continue

            # ── Cycle start: decrement per-symbol suspension counters ─────────
            self.symbols.decrement_suspensions()
            suspended_now = self.symbols.suspended_symbols()
            if suspended_now:
                logger.info(
                    f"⏸ Suspended symbols this cycle ({len(suspended_now)}): "
                    f"{', '.join(suspended_now)}")

            # ── Real-time queue – exclude suspended symbols ────────────────────
            current_queue = self.symbols.get_queue(max_symbols=200)
            ready = [
                s for s in current_queue
                if s in self._htf and not self.symbols.is_suspended(s)
            ]

            if not ready:
                await asyncio.sleep(5)
                continue

            update_status(current_symbol=f"[parallel] {len(ready)} symbols")

            # ── Scan all ready symbols simultaneously ─────────────────────────
            raw = await asyncio.gather(
                *[self._scan(s) for s in ready],
                return_exceptions=True)

            # ── Score and filter ──────────────────────────────────────────────
            min_score = getattr(config, "MIN_SIGNAL_SCORE", config.MIN_SIGNAL_PROBABILITY)
            candidates: List[ScanResult] = []
            for r in raw:
                if not isinstance(r, ScanResult) or r.sig.direction == "NONE":
                    continue
                r.prob_score = self._prob_score(r)
                if r.prob_score < min_score:
                    logger.debug(
                        f"Signal below threshold: {r.symbol} "
                        f"score={r.prob_score:.3f} < {min_score}")
                    continue
                candidates.append(r)

            if not candidates:
                await asyncio.sleep(SCAN_CYCLE_SLEEP)
                continue

            # ── Deduplicate: best signal per symbol ───────────────────────────
            best_per_symbol: Dict[str, ScanResult] = {}
            for r in candidates:
                sym = r.symbol
                if sym not in best_per_symbol or r.prob_score > best_per_symbol[sym].prob_score:
                    best_per_symbol[sym] = r

            # Sort unique-symbol signals by score descending
            unique_signals = sorted(best_per_symbol.values(),
                                    key=lambda r: r.prob_score, reverse=True)

            # ── Log full ranked list every cycle ─────────────────────────────
            logger.info(
                f"── Ranked signals this cycle ({len(unique_signals)} unique symbols, "
                f"{len(candidates)} total candidates) ──")
            for rank, r in enumerate(unique_signals, 1):
                conf      = getattr(r.sig, "confidence", "?")
                fresh     = getattr(r.smc_ctx, "zone_freshness", "?")
                str_score = (r.sig.strength / 3.0) * 4.0 * 0.40
                conf_score = (getattr(r.sig, "confidence", 0) / 7.0) * 4.0 * 0.35
                fresh_score = getattr(r.smc_ctx, "zone_freshness", 0.5) * 4.0 * 0.25
                logger.info(
                    f"  #{rank:>3} {r.symbol:<20} {r.sig.direction:<6} "
                    f"score={r.prob_score:.3f} "
                    f"[str={str_score:.3f} conf={conf_score:.3f} fresh={fresh_score:.3f}] "
                    f"strength={r.sig.strength}/3 confidence={conf}/7 "
                    f"freshness={fresh if isinstance(fresh, str) else f'{fresh:.2f}'}")
            logger.info(
                f"  streak={self.risk.current_streak} "
                f"effective_max_concurrent={self.risk.effective_max_concurrent} "
                f"suspended={len(suspended_now)}")

            # attempted_symbols: every symbol we tried to place (success OR fail)
            # executed_symbols:  only symbols where buy_contract confirmed a cid
            attempted_symbols: set = set()
            executed_symbols: set  = set()
            executed_count = 0

            # ── ROUND 1: one trade per unique symbol ──────────────────────────
            for sig_r in unique_signals:
                conf = getattr(sig_r.sig, "confidence", 0)
                if not self.risk.can_trade(
                        signal_strength=int(sig_r.sig.strength),
                        confidence=int(conf)):
                    break
                if sig_r.symbol in executed_symbols:
                    continue
                # Skip symbols already attempted but failed — they were counted
                # in attempted_symbols so we don't retry the same failure
                if sig_r.symbol in attempted_symbols:
                    continue

                attempted_symbols.add(sig_r.symbol)
                logger.info(
                    f"R1 execute: {sig_r.symbol} {sig_r.sig.direction} "
                    f"score={sig_r.prob_score:.3f} "
                    f"strength={sig_r.sig.strength}/3 "
                    f"confidence={conf}/7")
                update_status(
                    current_symbol = sig_r.symbol,
                    last_signal    = (f"[{sig_r.symbol}] "
                                      f"{sig_r.sig.direction} | {sig_r.sig.reason}"),
                )
                try:
                    success = await self._execute(
                        sig_r.symbol, sig_r.sig, sig_r.price, sig_r.smc_ctx)
                    if success:
                        executed_symbols.add(sig_r.symbol)
                        executed_count += 1
                    else:
                        # Placement failed — slot remains open; next ranked
                        # signal is the automatic substitute (loop continues)
                        logger.warning(
                            f"PLACEMENT FAILED R1: {sig_r.symbol} — "
                            f"slot NOT counted, next ranked signal is substitute")
                except Exception as exc:
                    logger.error(
                        f"Execute error R1 ({sig_r.symbol}): {exc}")
                    logger.warning(
                        f"SUBSTITUTE triggered after R1 exception on "
                        f"{sig_r.symbol} — continuing to next ranked signal")

            # ── ROUND 2: additional trades on already-traded symbols ──────────
            # Requires score > 2.5 AND strength == 3
            repeat_min_score = 2.5
            repeat_candidates = sorted(
                [r for r in candidates
                 if r.symbol in executed_symbols
                 and r.sig.strength >= 3
                 and r.prob_score > repeat_min_score],
                key=lambda r: r.prob_score, reverse=True)

            for sig_r in repeat_candidates:
                conf = getattr(sig_r.sig, "confidence", 0)
                if not self.risk.can_trade(
                        signal_strength=int(sig_r.sig.strength),
                        confidence=int(conf)):
                    break
                logger.info(
                    f"R2 execute (repeat symbol): {sig_r.symbol} "
                    f"{sig_r.sig.direction} score={sig_r.prob_score:.3f}")
                try:
                    success = await self._execute(
                        sig_r.symbol, sig_r.sig, sig_r.price, sig_r.smc_ctx)
                    if success:
                        executed_count += 1
                    else:
                        logger.warning(
                            f"PLACEMENT FAILED R2: {sig_r.symbol} — "
                            f"slot NOT counted, next R2 candidate is substitute")
                except Exception as exc:
                    logger.error(
                        f"Execute error R2 ({sig_r.symbol}): {exc}")
                    logger.warning(
                        f"SUBSTITUTE triggered after R2 exception on "
                        f"{sig_r.symbol} — continuing to next R2 candidate")

            if executed_count:
                logger.info(
                    f"Cycle complete: {executed_count} trade(s) placed | "
                    f"streak={self.risk.current_streak} | "
                    f"balance=${self.client.balance:.4f}")

            if executed_count:
                wait_secs = config.TRADE_DURATION * 60 + 10
                logger.info(
                    f"Trades placed – waiting {wait_secs}s for contracts to settle")
                await asyncio.sleep(wait_secs)

                # ── v13: increment completed-cycle counter ─────────────────────
                self._cycle_count += 1
                logger.info(
                    f"Cycle {self._cycle_count} settled | "
                    f"balance=${self.client.balance:.4f} | "
                    f"streak={self.risk.current_streak}")

                # ── v13: check redeploy threshold ─────────────────────────────
                redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 5)
                if self._cycle_count >= redeploy_every:
                    logger.info(
                        f"Redeploy scheduled after {self._cycle_count} completed "
                        f"cycle(s) — draining open contracts before restart")
                    set_redeploy_pending(True)
                    self._redeploy_triggered = True

                    drain_start = time.time()
                    while self._open_contracts:
                        elapsed = int(time.time() - drain_start)
                        logger.info(
                            f"Redeploy drain: {len(self._open_contracts)} "
                            f"contract(s) still open ({elapsed}s elapsed) — "
                            f"waiting for settlement")
                        await asyncio.sleep(10)

                    logger.info(
                        "All contracts drained — triggering Render redeploy hook")
                    _keep_alive_mod.trigger_redeploy()
                    logger.info(
                        "Redeploy hook fired — process sleeping 300 s then exiting. "
                        "Render will replace this instance with the new deploy.")
                    await asyncio.sleep(300)
                    return  # exits _main_loop; run() cleans up tasks and returns
            else:
                await asyncio.sleep(SCAN_CYCLE_SLEEP)

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
        Place a contract for symbol/sig/price/smc_ctx.

        Returns True  if the contract was successfully placed and confirmed
                       with a valid contract_id from the Deriv API.
        Returns False on any placement failure (None from buy_contract, missing
                       contract_id, or unexpected exception inside this method).
                       The caller MUST NOT increment executed_count or add the
                       symbol to executed_symbols when False is returned.
        """
        stake = self.risk.calculate_stake()
        ac    = get_symbol_class(symbol)

        conf    = getattr(sig, "confidence", "?")
        fresh   = getattr(smc_ctx, "zone_freshness", "?")
        logger.info(
            f"▶ {sig.direction} {symbol} | ${stake:.2f} | "
            f"struct={smc_ctx.structure} | modules={sig.strength}/3 | "
            f"confidence={conf}/7 | freshness={fresh if isinstance(fresh, str) else f'{fresh:.2f}'} | "
            f"streak={self.risk.current_streak}")

        # ── Direction inversion: flip signal before placing ───────────────────
        # Signal engine and SMC logic remain untouched and still report the
        # original direction above.  The inversion happens here, at the last
        # possible point, so all upstream logic is unaffected.
        inverted_direction = "SHORT" if sig.direction == "LONG" else "LONG"
        logger.info(
            f"Signal: {sig.direction} → Executing: {inverted_direction}"
        )

        buy_resp = await self.client.buy_contract(
            symbol    = symbol,
            direction = inverted_direction,
            stake     = stake,
            duration  = config.TRADE_DURATION,
            dur_unit  = config.TRADE_DURATION_UNIT,
        )

        # ── v13: treat None response as hard placement failure ────────────────
        if buy_resp is None:
            logger.warning(
                f"PLACEMENT FAILED: buy_contract returned None for {symbol} "
                f"({sig.direction} ${stake:.2f}) — slot NOT counted, "
                f"substitution will be attempted from next ranked signal")
            return False

        cid = str(buy_resp.get("contract_id", ""))
        if not cid:
            logger.warning(
                f"PLACEMENT FAILED: no contract_id in buy response for {symbol} "
                f"({sig.direction} ${stake:.2f}) — slot NOT counted, "
                f"substitution will be attempted from next ranked signal")
            return False

        bal_b = self.client.balance

        rec = self.risk.register_open(
            symbol=symbol, direction=sig.direction,
            stake=stake, entry_price=price)

        self.journal.open_trade(
            contract_id    = cid,
            symbol         = symbol,
            direction      = sig.direction,
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

        self._open_contracts[cid] = (symbol, sig.direction, stake, price, rec)
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

        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)

        self.journal.close_trade(
            contract_id   = cid,
            exit_price    = sell_price,
            pnl           = pnl,
            payout        = payout,
            balance_after = bal_after,
        )

        self.symbols.record_trade(symbol, won=pnl > 0, pnl=pnl)

        # ── Per-symbol cycle suspension ───────────────────────────────────────
        suspension_cycles = getattr(config, "SYMBOL_SUSPENSION_CYCLES", 2)
        if pnl < 0:
            self._confirmed_daily_loss += abs(pnl)
            self.symbols.suspend(symbol, cycles=suspension_cycles)
        else:
            self.symbols.clear_suspension(symbol)

        self._check_confirmed_loss_limit()
        set_active_trades(len(self._open_contracts))

        outcome = "✅ WIN" if pnl > 0 else "❌ LOSS"
        logger.info(
            f"{outcome} | {symbol} | pnl=${pnl:+.4f} | "
            f"balance=${bal_after:.4f} | streak={self.risk.current_streak} | "
            f"suspended_cycles={self.symbols.suspension_cycles_remaining(symbol)}")

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
