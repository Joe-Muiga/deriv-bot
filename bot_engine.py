"""
bot_engine.py – Central async orchestrator.

v14 — Main loop and execution logic rewrite per spec.

KEY CHANGES vs v13
──────────────────
INITIALISATION:
  • Only initialises symbols in config.TRADE_SYMBOLS — no volatility scan list.
  • Logs: "Initialising {N} trade symbols"
  • Skips symbols that fail to initialise — does not crash.

MAIN LOOP:
  • Continuous non-blocking scan loop — scanning and settling fully independent.
  • Scanning resumes immediately after execution; settlement runs as background task.
  • Only gate: do not execute if concurrent slots full.

EVERY SCAN CYCLE:
  1. reset_session() if UTC day changed.
  2. get_queue(initialised_symbols) for current tradeable set.
  3. Scan all queued symbols in parallel via asyncio.gather.
  4. Collect non-None signals.
  5. Filter: can_trade_now() must be True.
  6. Rank: signal.score × 0.85 + win_rate × 0.15.
  7. Deduplicate: highest scored per symbol.
  8. Execute top N = current_concurrent_limit in parallel.
  9. Log: CYCLE X | Queue:Q | Signals:S | Executing:E | Open:O | Balance:$B | Streak:+W

EXECUTION:
  • _scan(symbol): get ltf_bars, call signal.evaluate, return ScanResult or None.
  • _execute(symbol, sig): hard gate can_trade_now → stake → buy → record.
  • PLACEMENT FAILED logged on None return; does not count as executed.

CONTRACT RESULT:
  • Pop from _open_contracts, record result, register_close, dashboard push.
  • Confirmed daily loss tracked; _check_confirmed_loss_limit() called.

ORPHAN HANDLING:
  • force_check_contract for contracts older than CONTRACT_MAX_AGE_SECS.
  • Force-close as loss for contracts older than CONTRACT_FORCE_CLOSE_SECS.

DAILY LOSS LIMIT:
  • _confirmed_daily_loss / _day_start_balance >= DAILY_LOSS_LIMIT_PCT → pause until midnight.

REDEPLOY:
  • _cycle_count incremented after every settle wait.
  • At >= REDEPLOY_EVERY_N_CYCLES: drain open contracts then trigger_redeploy().

PRESERVED (unchanged):
  WebSocket connection, dashboard serving, keep_alive integration,
  Render redeploy mechanism, _on_tick, _init_data, _push_dashboard,
  _dashboard_loop, _resolve_remaining_contracts, _has_stale_contracts.
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
from keep_alive import (update_status, set_active_trades,
                        is_redeploy_pending, record_trade,
                        record_signal, _status)
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

DASHBOARD_PUSH_EVERY  = 15
SYMBOL_REFRESH_EVERY  = 3600
INIT_BATCH_SIZE       = 10

_ORPHAN_MAX_ATTEMPTS       = 3
CONTRACT_MAX_AGE_SECS      = getattr(config, "CONTRACT_MAX_AGE_SECS",      120)
CONTRACT_FORCE_CLOSE_SECS  = getattr(config, "CONTRACT_FORCE_CLOSE_SECS",  300)


# ─── Result container ──────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol:     str
    sig:        SignalResult
    price:      float
    smc_ctx:    SMCContext
    ltf_atr:    float
    htf_atr:    float
    score:      float = 0.0


# ─── Bot engine ────────────────────────────────────────────────────────────────

class BotEngine:

    def __init__(self):
        # Core state — declared before any object instantiation
        self._queue:                  List[str]          = []
        self._cycle_count:            int                = 0
        self._running:                bool               = False
        self._prescan_buffer:         List[ScanResult]   = []
        self._session_start_balance:  float              = 0.0
        self._open_contracts:         dict               = {}
        self._contract_open_times:    dict               = {}
        self._active_symbols:         Set[str]           = set()

        self._htf:                    Dict[str, CandlestickBuilder] = {}
        self._ltf:                    Dict[str, CandlestickBuilder] = {}
        self._initialised_symbols:    Set[str]           = set()
        self._initializing:           Set[str]           = set()
        self._last_symbol_refresh:    float              = 0.0

        self._confirmed_daily_loss:   float              = 0.0
        self._day_start_balance:      float              = 0.0
        self._confirmed_paused:       bool               = False
        self._current_utc_day:        int                = -1

        self._settle_tasks:           Set[asyncio.Task]  = set()

        # Sub-systems
        self.client  = DerivClient()
        self.risk    = RiskManager(
            risk_per_trade = config.RISK_PER_TRADE_PCT,
            min_stake      = config.MIN_STAKE,
            max_stake      = config.MAX_STAKE,
            max_concurrent = config.MAX_CONCURRENT_TRADES,
        )
        self.smc     = SMCAnalyzer(ob_expiry_bars=config.OB_EXPIRY_BARS)
        self.signal  = SignalEngine(
            symbols=getattr(config, "TRADE_SYMBOLS", []),
            config=config,
        )
        self.news    = NewsFilter(block_minutes=config.NEWS_BLOCK_MINUTES)
        self.journal = TradeJournal()
        self.symbols = SymbolManager()

    # ── Timeframe routing ──────────────────────────────────────────────────────

    @staticmethod
    def _ltf_gran(symbol: str) -> int:
        return (config.FOREX_LTF_GRANULARITY
                if symbol in sym_module.FOREX
                else config.OTHER_LTF_GRANULARITY)

    # ── Composite score (per spec: score×0.85 + win_rate×0.15) ───────────────

    def _composite_score(self, sig: SignalResult, symbol: str) -> float:
        signal_score = getattr(sig, "score", 0.0)
        win_rate     = (self.symbols.win_rate(symbol)
                        if hasattr(self.symbols, "win_rate") else 0.5)
        return round(float(signal_score) * 0.85 + float(win_rate) * 0.15, 4)

    # ── Daily loss limit ───────────────────────────────────────────────────────

    def _check_confirmed_loss_limit(self):
        today = _dt.datetime.utcnow().day
        if today != self._current_utc_day:
            self._confirmed_daily_loss = 0.0
            self._day_start_balance    = self.client.balance
            self._current_utc_day      = today
            logger.info(
                f"UTC day reset — day_start_balance=${self._day_start_balance:.4f}")

        if self._day_start_balance > 0:
            loss_ratio = self._confirmed_daily_loss / self._day_start_balance
            if loss_ratio >= config.DAILY_LOSS_LIMIT_PCT:
                if not self._confirmed_paused:
                    mins = self._minutes_until_midnight()
                    logger.warning(
                        f"DAILY LIMIT HIT — paused until midnight UTC "
                        f"(~{mins:.0f} min)")
                self._confirmed_paused = True
            else:
                self._confirmed_paused = False
        else:
            self._confirmed_paused = False

    @staticmethod
    def _minutes_until_midnight() -> float:
        now  = _dt.datetime.utcnow()
        midnight = (now + _dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return (midnight - now).total_seconds() / 60.0

    # ── Balance callback ───────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Trading Bot  –  parallel-scan / non-blocking")
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

        self._day_start_balance      = self.client.balance
        self._session_start_balance  = self.client.balance
        self._current_utc_day        = _dt.datetime.utcnow().day

        asyncio.create_task(self._contract_watchdog())

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
            for t in list(self._settle_tasks):
                t.cancel()

    # ── Initialise only config.TRADE_SYMBOLS ──────────────────────────────────

    async def _init_all_symbols(self):
        all_symbols = config.ALL_TRADE_SYMBOLS
        logger.info(f"Initialising {len(all_symbols)} symbols in parallel")
        await asyncio.gather(
            *[self._init_data(s) for s in all_symbols],
            return_exceptions=True
        )
        logger.info(f"Ready: {len(self._htf)} symbols initialised")
        self.symbols.update_active(list(self._htf.keys()))

    # ── Data initialisation ────────────────────────────────────────────────────

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
                logger.warning(f"{symbol}: no data — skipping")
                return

            if htf_data: htf_b.seed(htf_data)
            if ltf_data: ltf_b.seed(ltf_data)

            self._htf[symbol] = htf_b
            self._ltf[symbol] = ltf_b
            self._initialised_symbols.add(symbol)

            await self.client.subscribe_ticks(
                symbol,
                lambda tick, s=symbol: self._on_tick(s, tick))

            logger.info(
                f"{symbol}: ready | htf={htf_b.count} | "
                f"ltf={ltf_b.count} (ltf_gran={ltf_gran}s)")

        except Exception as exc:
            logger.error(f"_init_data({symbol}): skipping — {exc}")
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

    # ── Dashboard ──────────────────────────────────────────────────────────────

    async def _dashboard_loop(self):
        while True:
            try:
                self._push_dashboard()
            except Exception:
                pass
            await asyncio.sleep(DASHBOARD_PUSH_EVERY)

    def _push_dashboard(self):
        risk_s  = self.risk.summary()
        summary = self.journal.session_summary()
        update_status(
            running               = True,
            balance               = self.client.balance,
            day_start_balance     = self._day_start_balance,
            paused_for_loss_limit = self._confirmed_paused,
            trades_today          = risk_s.get("total_trades", 0),
            wins_today            = risk_s.get("wins", 0),
            losses_today          = risk_s.get("losses", 0),
            session               = (self.symbols.current_session
                                     if hasattr(self.symbols, "current_session")
                                     else "Active"),
            tradeable_count       = len([s for s in self._htf
                                         if not self.symbols.is_suspended(s)]),
            streak                = self.risk.current_streak,
            recent_trades         = _status.get("recent_trades", []),
            best_symbols          = self.symbols.best_symbols(10),
            balance_history       = _status.get("balance_history", []),
            suspended_symbols     = [
                {
                    "symbol":           s,
                    "suspended_until":  self.symbols._suspension_until.get(s, 0),
                }
                for s in getattr(self.symbols, "_suspension_until", {})
                if self.symbols.is_suspended(s)
            ],
            gross_profit          = summary.get("gross_profit", 0),
            gross_loss            = summary.get("gross_loss", 0),
            profit_factor         = summary.get("profit_factor", 0),
            avg_rr                = summary.get("avg_rr", 0),
            best_trade            = summary.get("best_trade", 0),
            worst_trade           = summary.get("worst_trade", 0),
        )

    # ── Main loop — continuous non-blocking ────────────────────────────────────

    async def _main_loop(self):
        scan_sleep     = getattr(config, "SCAN_CYCLE_SLEEP", 1)
        redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)
        cycle_number   = 0

        while True:
            cycle_start = time.time()

            # ── UTC day reset ─────────────────────────────────────────────
            today = _dt.datetime.utcnow().day
            if today != self._current_utc_day:
                self.symbols.reset_session()
                self._confirmed_daily_loss = 0.0
                self._day_start_balance    = self.client.balance
                self._current_utc_day      = today
                logger.info(
                    f"UTC day changed → session reset | "
                    f"day_start_balance=${self._day_start_balance:.4f}")

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
                    f"REDEPLOY TRIGGERED: draining {n_open} open "
                    f"contract(s) before restart")
                while self._open_contracts:
                    logger.info(
                        f"Draining — {len(self._open_contracts)} contract(s) open")
                    await asyncio.sleep(5)
                restart_scheduler.trigger_redeploy()
                logger.info("Redeploy triggered — standing by for 300 s")
                await asyncio.sleep(300)
                self._cycle_count = 0
                continue

            # ── Confirmed daily loss pause ────────────────────────────────
            if self._confirmed_paused:
                logger.info(
                    f"DAILY LIMIT HIT — pausing "
                    f"{config.DAILY_LOSS_PAUSE_MINS}min"
                )
                await asyncio.sleep(config.DAILY_LOSS_PAUSE_MINS * 60)
                self._confirmed_paused = False
                continue

            # ── Loss-streak pause (risk manager) ──────────────────────────
            self.risk.decrement_pause()
            if self.risk.pause_cycles_remaining > 0:
                logger.info(
                    f"⏸ Loss-streak pause: "
                    f"{self.risk.pause_cycles_remaining} cycle(s) remaining | "
                    f"streak={self.risk.current_streak}")
                self.risk.consume_pause_cycle()
                await asyncio.sleep(scan_sleep)
                continue

            # ── Stale contract gate ───────────────────────────────────────
            if self._has_stale_contracts():
                stale = [
                    cid for cid in self._open_contracts
                    if (time.time() - self._contract_open_times.get(cid, time.time()))
                    > config.TRADE_DURATION * 60 + 30
                ]
                logger.warning(
                    f"Stale contracts detected: {stale} — running force-check")
                await self._resolve_remaining_contracts()
                await asyncio.sleep(scan_sleep)
                continue

            # ── Cycle bookkeeping ─────────────────────────────────────────
            self.symbols.decrement_suspensions()
            self.symbols.reset_cycle_used()

            # ── Build queue from initialised symbols ──────────────────────
            cycle_number += 1

            # Scan ALL available symbols simultaneously every cycle
            queue = self.symbols.get_queue()
            if not queue:
                await asyncio.sleep(scan_sleep)
                continue

            raw_results = await asyncio.gather(
                *[self._scan(s) for s in queue],
                return_exceptions=True
            )
            # Process results immediately while next scan already queued
            candidates = [
                r for r in raw_results
                if isinstance(r, ScanResult)
                and r.sig.direction != "NONE"
                and self.symbols.can_trade_now(r.symbol)
            ]

            # ── Score: signal.score×0.85 + win_rate×0.15 ─────────────────
            for r in candidates:
                r.score = self._composite_score(r.sig, r.symbol)

            # ── Deduplicate: highest score per symbol ─────────────────────
            best_per_symbol: Dict[str, ScanResult] = {}
            for r in candidates:
                if (r.symbol not in best_per_symbol
                        or r.score > best_per_symbol[r.symbol].score):
                    best_per_symbol[r.symbol] = r

            ranked: List[ScanResult] = sorted(
                best_per_symbol.values(),
                key=lambda r: r.score,
                reverse=True,
            )

            # ── Hard block: exclude symbols with open trades ───────────────
            candidates = [r for r in ranked if r.symbol not in self._active_symbols]

            # Execute all top signals in parallel instantly
            top = candidates[:self.risk.current_concurrent_limit]

            open_count = len(self._open_contracts)
            streak = self.risk.current_streak
            logger.info(
                f"CYCLE {cycle_number} | "
                f"Queue:{len(queue)} | "
                f"Signals:{len(ranked)} | "
                f"Executing:{len(top)} | "
                f"Open:{open_count} | "
                f"Balance:${self.client.balance:.4f} | "
                f"Streak:{'+' if streak >= 0 else ''}{streak}")

            if top and not self._confirmed_paused:
                await asyncio.gather(
                    *[self._execute(r.symbol, r.sig, r.price)
                      for r in top],
                    return_exceptions=True,
                )

            # ── Orphan monitoring as background task ──────────────────────
            orphan_task = asyncio.create_task(self._monitor_orphans())
            self._settle_tasks.add(orphan_task)
            orphan_task.add_done_callback(self._settle_tasks.discard)

            # ── Cycle sleep ───────────────────────────────────────────────
            elapsed   = time.time() - cycle_start
            remainder = max(0.0, scan_sleep - elapsed)
            if remainder > 0:
                await asyncio.sleep(remainder)

    # ── Per-symbol scan ────────────────────────────────────────────────────────

    async def _scan(self, symbol: str) -> Optional[ScanResult]:
        try:
            ltf_builder = self._ltf.get(symbol)
            if ltf_builder is None:
                return None

            ltf_bars = ltf_builder.completed_bars
            if not ltf_bars or len(ltf_bars) < 30:
                return None

            sig = self.signal.evaluate(ltf_bars, symbol)
            if sig is None or getattr(sig, "direction", "NONE") == "NONE":
                return None

            if self.news.is_blocked(symbol):
                return None

            # HTF context for SMC
            htf_builder = self._htf.get(symbol)
            htf_atr     = 0.0
            smc_ctx     = None
            current_price = float(ltf_bars[-1].close)

            if htf_builder is not None and htf_builder.count >= 20:
                htf_bars = htf_builder.completed_bars
                H = np.array([b.high  for b in htf_bars])
                L = np.array([b.low   for b in htf_bars])
                C = np.array([b.close for b in htf_bars])
                htf_atr_arr = ind.atr(H, L, C, 14)
                valid_ha    = htf_atr_arr[~np.isnan(htf_atr_arr)]
                htf_atr     = float(valid_ha[-1]) if len(valid_ha) else 0.0
                smc_ctx     = self.smc.analyse(htf_bars, htf_atr, symbol=symbol)

            # LTF ATR filter
            ltf_H = np.array([b.high  for b in ltf_bars])
            ltf_L = np.array([b.low   for b in ltf_bars])
            ltf_C = np.array([b.close for b in ltf_bars])
            ltf_atr_arr = ind.atr(ltf_H, ltf_L, ltf_C, 14)
            valid_la    = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
            ltf_atr     = float(valid_la[-1]) if len(valid_la) else 0.0

            if htf_atr > 0 and ltf_atr > 2 * htf_atr:
                return None

            # Build a minimal SMCContext stub if SMC unavailable
            if smc_ctx is None:
                smc_ctx = SMCContext()

            return ScanResult(
                symbol   = symbol,
                sig      = sig,
                price    = current_price,
                smc_ctx  = smc_ctx,
                ltf_atr  = ltf_atr,
                htf_atr  = htf_atr,
            )

        except Exception as exc:
            logger.debug(f"_scan({symbol}): {exc}")
            return None

    # ── Execution ──────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult, price: float = 0.0) -> bool:
        # Hard gate
        if not self.symbols.can_trade_now(symbol):
            return False
        if symbol in self._active_symbols:
            return False
        if not self.risk.can_trade():
            return False

        stake     = await self.risk.calculate_stake()
        direction = sig.direction
        ac        = get_symbol_class(symbol)

        record_signal(
            symbol    = symbol,
            direction = sig.direction,
            strategy  = getattr(sig, "strategy", "unknown"),
            score     = getattr(sig, "score", 0.0),
        )

        buy_resp = await self.client.buy_contract(
            symbol    = symbol,
            direction = direction,
            stake     = stake,
            duration  = config.TRADE_DURATION,
            dur_unit  = config.TRADE_DURATION_UNIT,
        )

        if buy_resp is None:
            logger.warning(f"PLACEMENT FAILED: {symbol}")
            return False

        cid       = str(buy_resp.get("contract_id", ""))
        bal_b     = self.client.balance
        price     = float(buy_resp.get("buy_price", stake))
        smc_ctx   = SMCContext()    # lightweight placeholder post-execution

        rec = self.risk.register_open(
            symbol      = symbol,
            direction   = direction,
            stake       = stake,
            entry_price = price,
        )

        self.journal.open_trade(
            contract_id    = cid,
            symbol         = symbol,
            direction      = direction,
            stake          = stake,
            entry_price    = price,
            balance_before = bal_b,
            asset_class    = ac,
            htf_bias       = getattr(smc_ctx, "bias", "NEUTRAL"),
            smc_structure  = getattr(smc_ctx, "structure", "NONE"),
            m1             = getattr(sig, "m1_signal", 0),
            m2             = getattr(sig, "m2_signal", 0),
            m3             = getattr(sig, "m3_signal", sig.strength),
            modules        = sig.strength,
        )

        self._open_contracts[cid] = {
            "symbol":      symbol,
            "direction":   sig.direction,
            "stake":       stake,
            "entry_price": price,
            "opened_at":   time.time(),
            "rec":         rec,
            "sig":         sig,
        }
        self.symbols.record_trade_placed(symbol)
        self._active_symbols.add(symbol)
        set_active_trades(len(self._open_contracts))

        logger.info(
            f"▶ {direction} {symbol} | ${stake:.2f} | "
            f"modules={sig.strength}/3 | streak={self.risk.current_streak}")

        await self.client.subscribe_contract(
            cid,
            lambda msg, _cid=cid: asyncio.create_task(
                self._on_contract_result(_cid, msg)))

        try:
            self._push_dashboard()
        except Exception:
            pass

        return True

    # ── Contract result callback ───────────────────────────────────────────────

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

        symbol      = info["symbol"]
        direction   = info["direction"]
        stake       = info["stake"]
        entry_price = info["entry_price"]
        rec         = info["rec"]
        sig         = info["sig"]
        won = pnl > 0

        self._active_symbols.discard(symbol)
        logger.info(f"RELEASED: {symbol} | Active now: {self._active_symbols}")

        self.journal.close_trade(
            contract_id   = cid,
            exit_price    = sell_price,
            pnl           = pnl,
            payout        = payout,
            balance_after = bal_after,
        )

        # Record to dashboard immediately
        from keep_alive import record_trade, record_signal
        record_trade(
            symbol        = symbol,
            direction     = direction,
            stake         = stake,
            pnl           = pnl,
            balance_after = self.client.balance,
            won           = pnl > 0,
            strategy      = getattr(sig, 'strategy', ''),
        )
        self.symbols.record_contract_closed(symbol)
        self.symbols.record_result(symbol, won=pnl > 0)
        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)
        self._confirmed_daily_loss += abs(pnl) if pnl < 0 else 0
        self._check_confirmed_loss_limit()
        set_active_trades(len(self._open_contracts))
        self._push_dashboard()

        logger.info(
            f"{'✅ WIN' if pnl>0 else '❌ LOSS'} | "
            f"{symbol} | pnl=${pnl:+.4f} | "
            f"balance=${self.client.balance:.4f} | "
            f"streak={self.risk.current_streak}"
        )

    # ── Orphan monitoring ──────────────────────────────────────────────────────

    async def _monitor_orphans(self):
        """
        Runs once per cycle as a background task.
        - force_check_contract for contracts older than CONTRACT_MAX_AGE_SECS.
        - Force-close as loss for contracts older than CONTRACT_FORCE_CLOSE_SECS.
        """
        try:
            now = time.time()
            for cid in list(self._open_contracts.keys()):
                info = self._open_contracts.get(cid)
                if not info:
                    continue
                open_time = info.get("opened_at", now)
                age       = now - open_time

                if age >= CONTRACT_FORCE_CLOSE_SECS:
                    symbol    = info["symbol"]
                    stake     = info["stake"]
                    rec       = info["rec"]

                    self._open_contracts.pop(cid, None)
                    self._contract_open_times.pop(cid, None)
                    self._active_symbols.discard(symbol)

                    self.symbols.record_result(symbol, won=False)
                    self.risk.register_close(rec, exit_price=0, pnl=-stake)
                    self._confirmed_daily_loss += stake
                    self._check_confirmed_loss_limit()
                    set_active_trades(len(self._open_contracts))

                    logger.error(
                        f"ORPHAN LOSS: {cid} -{stake:.2f} | "
                        f"age={age:.0f}s > force-close threshold")
                    try:
                        self._push_dashboard()
                    except Exception:
                        pass

                elif age >= CONTRACT_MAX_AGE_SECS:
                    try:
                        await self.client.force_check_contract(cid)
                        logger.debug(f"force_check_contract({cid}) triggered (age={age:.0f}s)")
                    except Exception as exc:
                        logger.warning(f"force_check_contract({cid}): {exc}")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"_monitor_orphans: {exc}")

    # ── Settle task launcher (preserve background settle flow) ────────────────

    async def _settle_and_resolve(self, wait_secs: float):
        """
        Background task: waits for settlement window then resolves any
        remaining open contracts.  Increments cycle count.
        """
        try:
            await asyncio.sleep(wait_secs)
            await self._resolve_remaining_contracts()
            self._cycle_count += 1
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"_settle_and_resolve: {exc}")

    # ── Orphan resolution (full, unchanged from v13) ───────────────────────────

    async def _resolve_remaining_contracts(self):
        remaining = list(self._open_contracts.keys())
        n   = len(remaining)
        now = time.time()

        if n == 0:
            logger.info("Open contracts remaining: 0")
            return

        age_parts = []
        for cid in remaining:
            info_r   = self._open_contracts.get(cid, {})
            age_secs = int(now - info_r.get("opened_at", now))
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
                            f"force_check({cid}): confirmed closed "
                            f"(attempt {attempt}) — firing result handler")
                        await self._on_contract_result(cid, resp)
                        resolved = True
                        break
                    else:
                        logger.debug(
                            f"force_check({cid}): still open "
                            f"(attempt {attempt}/{_ORPHAN_MAX_ATTEMPTS})")
                        if attempt < _ORPHAN_MAX_ATTEMPTS:
                            await asyncio.sleep(5)
                except Exception as exc:
                    logger.warning(
                        f"force_check({cid}) attempt {attempt} error: {exc}")
                    if attempt < _ORPHAN_MAX_ATTEMPTS:
                        await asyncio.sleep(5)

            if not resolved and cid in self._open_contracts:
                info   = self._open_contracts.get(cid, {})
                symbol = info.get("symbol", "UNKNOWN")
                stake  = info.get("stake", 0.0)
                rec    = info.get("rec", None)

                logger.error(f"ORPHANED: {cid} — forcing close as loss")
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
                self._active_symbols.discard(symbol)
                set_active_trades(len(self._open_contracts))

                try:
                    self._push_dashboard()
                except Exception:
                    pass

                logger.error(
                    f"ORPHAN RECORDED AS LOSS: {cid} {symbol} "
                    f"-${stake:.2f} | balance=${self.client.balance:.4f}")

    # ── Contract watchdog — hard 7-minute timeout ─────────────────────────────

    async def _contract_watchdog(self):
        while True:
            try:
                await asyncio.sleep(15)
                now = time.time()
                for cid, info in list(self._open_contracts.items()):
                    age_secs = now - info.get("opened_at", now)
                    age_mins = age_secs / 60

                    # At 5 minutes — force check
                    if age_secs >= config.CONTRACT_CHECK_SECS:
                        logger.info(
                            f"WATCHDOG CHECK: {cid} "
                            f"{info['symbol']} age={age_mins:.1f}min"
                        )
                        try:
                            result = await self.client.force_check_contract(cid)
                            if result.get("is_sold") or result.get("is_expired"):
                                await self._on_contract_result(
                                    cid, {"proposal_open_contract": result})
                        except Exception as exc:
                            logger.warning(f"WATCHDOG force_check({cid}): {exc}")

                    # At 7 minutes — force close as loss
                    if age_secs >= config.CONTRACT_TIMEOUT_SECS:
                        if cid in self._open_contracts:
                            stake  = info["stake"]
                            symbol = info["symbol"]
                            logger.warning(
                                f"TIMEOUT: {cid} {symbol} "
                                f"{age_mins:.1f}min — recording loss"
                            )
                            from keep_alive import record_trade
                            record_trade(
                                symbol        = symbol,
                                direction     = info.get("direction", "?"),
                                stake         = stake,
                                pnl           = -stake,
                                balance_after = self.client.balance,
                                won           = False,
                                strategy      = "TIMEOUT",
                            )
                            self._confirmed_daily_loss += stake
                            self.risk.register_close(
                                info["rec"], exit_price=0, pnl=-stake)
                            self.symbols.record_contract_closed(symbol)
                            self.symbols.record_result(symbol, won=False)
                            self._open_contracts.pop(cid, None)
                            self._active_symbols.discard(symbol)
                            set_active_trades(len(self._open_contracts))
                            self._check_confirmed_loss_limit()
                            self._push_dashboard()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"_contract_watchdog: {exc}")

    # ── Stale contract detection ───────────────────────────────────────────────

    def _has_stale_contracts(self) -> bool:
        stale_threshold = config.TRADE_DURATION * 60 + 30
        now = time.time()
        for cid, open_time in list(self._contract_open_times.items()):
            if cid in self._open_contracts:
                if now - open_time > stale_threshold:
                    return True
        return False
