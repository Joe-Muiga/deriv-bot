"""
bot_engine.py – Central async orchestrator.

v16 — Dashboard push every 5 s; full open-contract dicts pushed to dashboard;
      record_signal() called on every OPEN; record_trade() called on every CLOSE
      and every TIMEOUT; record_failure() called on every failed placement;
      _push_dashboard() called after every state change.

KEY CHANGES vs v15
──────────────────
DASHBOARD:
  • DASHBOARD_PUSH_EVERY = 5  (was 15)
  • _push_dashboard() now builds full open-contract dicts (symbol, direction,
    stake, opened_at) and passes them via update_open_contracts().
  • _push_dashboard() also passes open_contracts_count and active_trades.

TRADE OPEN (_execute):
  • Calls record_signal() right before buy_contract (was already present).
  • Calls _push_dashboard() immediately after contract is registered open.

TRADE CLOSE (_on_contract_result):
  • record_trade() call updated: passes multiplier and close_reason="normal".
  • _push_dashboard() called immediately after.

TIMEOUT / FUNDING TIMEOUT (_contract_watchdog):
  • Both timeout branches call record_trade(..., close_reason="timeout").
  • Both call _push_dashboard() immediately after.

PLACEMENT FAILURE (_execute):
  • buy_resp is None → calls record_failure() with symbol, direction, stake,
    strategy, reason="buy_resp=None".
  • _push_dashboard() called on failure too.

ORPHAN LOSS (_monitor_orphans, _resolve_remaining_contracts):
  • Both orphan paths call record_trade() with close_reason="timeout" and
    _push_dashboard() after.

ALL OTHER logic, routes, timeframes, scoring, redeploy, and init are unchanged.
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
                        record_signal, record_failure,
                        update_open_contracts, _status)
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

DASHBOARD_PUSH_EVERY  = 5        # push every 5 seconds
SYMBOL_REFRESH_EVERY  = 3600
INIT_BATCH_SIZE       = getattr(config, "INIT_BATCH_SIZE", 10)

_ORPHAN_MAX_ATTEMPTS      = 3
CONTRACT_MAX_AGE_SECS     = getattr(config, "CONTRACT_MAX_AGE_SECS",      120)
CONTRACT_FORCE_CLOSE_SECS = getattr(config, "CONTRACT_FORCE_CLOSE_SECS",  300)

_PHASE1_HTF_BARS = 20
_PHASE1_MTF_BARS = 20
_PHASE1_LTF_BARS = 30


# ─── Result container ──────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol:  str
    sig:     SignalResult
    price:   float
    smc_ctx: SMCContext
    score:   float = 0.0


# ─── Bot engine ────────────────────────────────────────────────────────────────

class BotEngine:

    def __init__(self):
        self._queue:                  List[str]          = []
        self._cycle_count:            int                = 0
        self._running:                bool               = False
        self._prescan_buffer:         List[ScanResult]   = []
        self._session_start_balance:  float              = 0.0
        self._open_contracts:         dict               = {}
        self._contract_open_times:    dict               = {}
        self._active_symbols:         Set[str]           = set()

        self._htf:                    Dict[str, CandlestickBuilder] = {}
        self._mtf:                    Dict[str, CandlestickBuilder] = {}
        self._ltf:                    Dict[str, CandlestickBuilder] = {}

        self._initialised_symbols:    Set[str]           = set()
        self._initializing:           Set[str]           = set()
        self._last_symbol_refresh:    float              = 0.0

        self._confirmed_daily_loss:   float              = 0.0
        self._day_start_balance:      float              = 0.0
        self._confirmed_paused:       bool               = False
        self._current_utc_day:        int                = -1

        self._settle_tasks:           Set[asyncio.Task]  = set()

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

    @staticmethod
    def _mtf_gran(symbol: str) -> int:
        return getattr(
            config,
            "FOREX_MTF_GRANULARITY" if symbol in sym_module.FOREX else "OTHER_MTF_GRANULARITY",
            getattr(config, "MTF_GRANULARITY", 300),
        )

    # ── Composite score ────────────────────────────────────────────────────────

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
        now      = _dt.datetime.utcnow()
        midnight = (now + _dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return (midnight - now).total_seconds() / 60.0

    # ── Balance callback ───────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Trading Bot  –  multiplier / parallel-scan")
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

        self._day_start_balance     = self.client.balance
        self._session_start_balance = self.client.balance
        self._current_utc_day       = _dt.datetime.utcnow().day

        asyncio.create_task(self._contract_watchdog())
        asyncio.create_task(self._symbol_release_watchdog())

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

    # ── Two-phase + upgrade startup ────────────────────────────────────────────

    async def _init_all_symbols(self):
        priority   = list(getattr(config, "PRIORITY_SYMBOLS", []))
        all_syms   = list(config.ALL_TRADE_SYMBOLS)
        remaining  = [s for s in all_syms if s not in priority]
        batch_size = getattr(config, "INIT_BATCH_SIZE",  INIT_BATCH_SIZE)
        batch_delay= getattr(config, "INIT_BATCH_DELAY", 2.0)

        logger.info(
            f"Phase 1: initialising {len(priority)} priority symbol(s) "
            f"(htf={_PHASE1_HTF_BARS}, mtf={_PHASE1_MTF_BARS}, ltf={_PHASE1_LTF_BARS})")
        await asyncio.gather(
            *[self._init_data(s,
                              htf_bars=_PHASE1_HTF_BARS,
                              mtf_bars=_PHASE1_MTF_BARS,
                              ltf_bars=_PHASE1_LTF_BARS)
              for s in priority],
            return_exceptions=True,
        )
        logger.info(
            f"Phase 1 complete — {len(self._htf)} symbol(s) ready; "
            f"trading now active")
        self.symbols.update_active(list(self._htf.keys()))

        asyncio.create_task(
            self._background_init(remaining, batch_size, batch_delay))

    async def _background_init(
        self,
        remaining:   List[str],
        batch_size:  int,
        batch_delay: float,
    ):
        for i in range(0, len(remaining), batch_size):
            batch = remaining[i : i + batch_size]
            logger.info(
                f"Phase 2 batch {i // batch_size + 1}: "
                f"initialising {len(batch)} symbol(s)")
            await asyncio.gather(
                *[self._init_data(s,
                                  htf_bars=_PHASE1_HTF_BARS,
                                  mtf_bars=_PHASE1_MTF_BARS,
                                  ltf_bars=_PHASE1_LTF_BARS)
                  for s in batch],
                return_exceptions=True,
            )
            self.symbols.update_active(list(self._htf.keys()))
            if i + batch_size < len(remaining):
                await asyncio.sleep(batch_delay)

        logger.info(f"Phase 2 complete — {len(self._htf)} symbol(s) ready")

        all_ready = list(self._htf.keys())
        logger.info(f"Phase 3: upgrading {len(all_ready)} symbol(s) to full bar count")
        for s in all_ready:
            try:
                await self._upgrade_symbol(s)
            except Exception as exc:
                logger.debug(f"Phase 3 upgrade({s}): {exc}")
            await asyncio.sleep(0.1)

        logger.info("Phase 3 complete — all symbols at full resolution")

    async def _upgrade_symbol(self, symbol: str):
        ltf_gran = self._ltf_gran(symbol)
        mtf_gran = self._mtf_gran(symbol)

        htf_data, mtf_data, ltf_data = await asyncio.gather(
            self.client.get_candles(symbol, config.HTF_GRANULARITY, config.HTF_BARS),
            self.client.get_candles(symbol, mtf_gran,               config.MTF_BARS),
            self.client.get_candles(symbol, ltf_gran,               config.LTF_BARS),
            return_exceptions=True,
        )

        if isinstance(htf_data, Exception): htf_data = []
        if isinstance(mtf_data, Exception): mtf_data = []
        if isinstance(ltf_data, Exception): ltf_data = []

        if htf_data and symbol in self._htf: self._htf[symbol].seed(htf_data)
        if mtf_data and symbol in self._mtf: self._mtf[symbol].seed(mtf_data)
        if ltf_data and symbol in self._ltf: self._ltf[symbol].seed(ltf_data)

    # ── Data initialisation — three TFs simultaneously ─────────────────────────

    async def _init_data(
        self,
        symbol:   str,
        htf_bars: int = None,
        mtf_bars: int = None,
        ltf_bars: int = None,
    ):
        if symbol in self._initializing or symbol in self._htf:
            return
        self._initializing.add(symbol)

        if htf_bars is None: htf_bars = config.HTF_BARS
        if mtf_bars is None: mtf_bars = getattr(config, "MTF_BARS", 50)
        if ltf_bars is None: ltf_bars = config.LTF_BARS

        try:
            ltf_gran = self._ltf_gran(symbol)
            mtf_gran = self._mtf_gran(symbol)

            htf_b = CandlestickBuilder(granularity=config.HTF_GRANULARITY,
                                       max_bars=htf_bars + 20)
            mtf_b = CandlestickBuilder(granularity=mtf_gran,
                                       max_bars=mtf_bars + 20)
            ltf_b = CandlestickBuilder(granularity=ltf_gran,
                                       max_bars=ltf_bars + 20)

            htf_data, mtf_data, ltf_data = await asyncio.gather(
                self.client.get_candles(symbol, config.HTF_GRANULARITY, htf_bars),
                self.client.get_candles(symbol, mtf_gran,               mtf_bars),
                self.client.get_candles(symbol, ltf_gran,               ltf_bars),
                return_exceptions=True,
            )

            if isinstance(htf_data, Exception): htf_data = []
            if isinstance(mtf_data, Exception): mtf_data = []
            if isinstance(ltf_data, Exception): ltf_data = []

            if not htf_data and not ltf_data:
                logger.warning(f"{symbol}: no data — skipping")
                return

            if htf_data: htf_b.seed(htf_data)
            if mtf_data: mtf_b.seed(mtf_data)
            if ltf_data: ltf_b.seed(ltf_data)

            self._htf[symbol] = htf_b
            self._mtf[symbol] = mtf_b
            self._ltf[symbol] = ltf_b
            self._initialised_symbols.add(symbol)

            await self.client.subscribe_ticks(
                symbol,
                lambda tick, s=symbol: self._on_tick(s, tick))

            logger.info(
                f"{symbol}: ready | htf={htf_b.count} | "
                f"mtf={mtf_b.count} | ltf={ltf_b.count} "
                f"(ltf_gran={ltf_gran}s)")

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
        for store in (self._ltf, self._mtf, self._htf):
            if symbol in store:
                store[symbol].add_tick(epoch, price)

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

        # Build full open-contract dicts for dashboard rendering
        oc_list = []
        now_ts  = time.time()
        for cid, info in self._open_contracts.items():
            oc_list.append({
                "contract_id": cid,
                "symbol":      info.get("symbol",    "—"),
                "direction":   info.get("direction",  "—"),
                "stake":       info.get("stake",       0.0),
                "opened_at":   info.get("opened_at",  now_ts),
                "strategy":    info.get("strategy",   ""),
                "multiplier":  info.get("multiplier",  None),
            })

        try:
            update_open_contracts(oc_list)
        except Exception:
            pass

        update_status(
            running               = True,
            balance               = self.client.balance,
            day_start_balance     = self._day_start_balance,
            session_start_balance = self._session_start_balance,
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
            best_symbols          = self.symbols.best_symbols(50),
            balance_history       = _status.get("balance_history", []),
            suspended_symbols     = [
                {
                    "symbol":          s,
                    "suspended_until": self.symbols._suspension_until.get(s, 0),
                }
                for s in getattr(self.symbols, "_suspension_until", {})
                if self.symbols.is_suspended(s)
            ],
            gross_profit          = summary.get("gross_profit",  0),
            gross_loss            = summary.get("gross_loss",    0),
            profit_factor         = summary.get("profit_factor", 0),
            avg_rr                = summary.get("avg_rr",        0),
            best_trade            = _status.get("best_trade",    summary.get("best_trade",  0)),
            worst_trade           = _status.get("worst_trade",   summary.get("worst_trade", 0)),
            open_contracts_count  = len(oc_list),
            active_trades         = len(self._open_contracts),
        )

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _main_loop(self):
        scan_sleep     = getattr(config, "SCAN_CYCLE_SLEEP",        1)
        redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)
        cycle_number   = 0

        while True:
            cycle_start = time.time()

            # ── Fix 4: never let _active_symbols block all trading ─────────────
            for sym in list(self._active_symbols):
                has_open = any(
                    v.get("symbol") == sym
                    for v in self._open_contracts.values())
                if not has_open:
                    logger.warning(f"STALE ACTIVE SYMBOL: {sym} — clearing")
                    self._active_symbols.discard(sym)

            today = _dt.datetime.utcnow().day
            if today != self._current_utc_day:
                self.symbols.reset_session()
                self._confirmed_daily_loss = 0.0
                self._day_start_balance    = self.client.balance
                self._current_utc_day      = today
                logger.info(
                    f"UTC day changed → session reset | "
                    f"day_start_balance=${self._day_start_balance:.4f}")

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

            if self._confirmed_paused:
                logger.info(
                    f"DAILY LIMIT HIT — pausing "
                    f"{config.DAILY_LOSS_PAUSE_MINS}min")
                await asyncio.sleep(config.DAILY_LOSS_PAUSE_MINS * 60)
                self._confirmed_paused = False
                continue

            self.risk.decrement_pause()
            if self.risk.pause_cycles_remaining > 0:
                logger.info(
                    f"⏸ Loss-streak pause: "
                    f"{self.risk.pause_cycles_remaining} cycle(s) remaining | "
                    f"streak={self.risk.current_streak}")
                self.risk.consume_pause_cycle()
                await asyncio.sleep(scan_sleep)
                continue

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

            self.symbols.decrement_suspensions()
            cycle_number += 1

            queue = self.symbols.get_queue()
            logger.info(
                f"CYCLE | Queue:{len(queue)} | "
                f"Active:{len(self._active_symbols)} | "
                f"Open:{len(self._open_contracts)} | "
                f"Balance:${self.client.balance:.2f}")
            if not queue:
                await asyncio.sleep(scan_sleep)
                continue

            # Pre-compute stake once — avoids N redundant awaits inside gather
            cycle_stake = await self.risk.calculate_stake()
            t_gather0   = time.perf_counter()
            raw_results = await asyncio.gather(
                *[self._scan(s, precomputed_stake=cycle_stake) for s in queue],
                return_exceptions=True,
            )
            logger.info(
                f"SCAN_GATHER {len(queue)} symbols in "
                f"{(time.perf_counter() - t_gather0)*1000:.1f}ms")
            candidates = [
                r for r in raw_results
                if r is not None
                and isinstance(r, ScanResult)
                and r.sig.direction != "NONE"
                and r.sig.score >= 0.35
                and self.symbols.can_trade_now(r.symbol)
            ]

            for r in candidates:
                r.score = self._composite_score(r.sig, r.symbol)

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

            candidates = [r for r in ranked if r.symbol not in self._active_symbols]

            # ── Rank all signals by score descending ──────────────────────────
            candidates = sorted(
                candidates,
                key=lambda r: r.sig.score,
                reverse=True
            )

            # Deduplicate — one signal per symbol
            seen = set()
            unique = []
            for r in candidates:
                if r.symbol not in seen:
                    unique.append(r)
                    seen.add(r.symbol)
            candidates = unique

            # Log full ranking
            logger.info(f"RANKED SIGNALS THIS CYCLE:")
            for i, r in enumerate(candidates[:10]):
                logger.info(
                    f"  #{i+1} {r.symbol} {r.sig.direction} "
                    f"score={r.sig.score:.3f} "
                    f"strategy={r.sig.strategy}")

            # Execute only top N above threshold
            top = [r for r in candidates
                   if r.sig.score >= config.MIN_SIGNAL_SCORE
                   ][:self.risk.current_concurrent_limit]

            if not top:
                logger.info("NO QUALIFYING SIGNALS THIS CYCLE")

            open_count = len(self._open_contracts)
            streak     = self.risk.current_streak
            logger.info(
                f"CYCLE {cycle_number} | "
                f"Queue:{len(queue)} | "
                f"Signals:{len(candidates)} | "
                f"Executing:{len(top)} | "
                f"Open:{open_count} | "
                f"Balance:${self.client.balance:.4f} | "
                f"Streak:{'+' if streak >= 0 else ''}{streak}")

            if top and not self._confirmed_paused:
                for sig_r in top:
                    await self._execute(sig_r.symbol, sig_r.sig, sig_r.price, sig_r.smc_ctx)
                    await asyncio.sleep(2)  # 2 second gap between each trade

            orphan_task = asyncio.create_task(self._monitor_orphans())
            self._settle_tasks.add(orphan_task)
            orphan_task.add_done_callback(self._settle_tasks.discard)

            elapsed   = time.time() - cycle_start
            remainder = max(0.0, scan_sleep - elapsed)
            if remainder > 0:
                await asyncio.sleep(remainder)

    # ── Per-symbol scan ────────────────────────────────────────────────────────
    #
    # PARALLELISM NOTE  (Option 3 audit)
    # -----------------------------------
    # _scan() is called via asyncio.gather(), which runs all coroutines
    # concurrently on the *same* thread.  That means any synchronous CPU work
    # inside _scan() (e.g. signal.evaluate()) blocks the event loop for every
    # symbol until it finishes — the gather gives you concurrency only if each
    # coroutine actually yields (awaits I/O).  Two issues were found and fixed:
    #
    #   1. calculate_stake() was awaited once *per symbol* inside the gather.
    #      It returns the same value for every symbol in a cycle, so it is now
    #      computed once before the gather and passed in as a parameter.
    #
    #   2. signal.evaluate() is synchronous CPU work.  Running it directly
    #      inside the gather means each symbol blocks the loop in turn, making
    #      the "parallel" gather effectively serial.  It is now offloaded to the
    #      default ThreadPoolExecutor via asyncio.get_event_loop().run_in_executor()
    #      so each symbol's evaluation genuinely runs concurrently.
    #
    # TIMING INSTRUMENTATION  (Option 4)
    # ------------------------------------
    # Every call logs:
    #   SCAN_TIMING  <symbol>  total=Xms  eval=Xms  stake=Xms
    # Scan hits additionally log their direction / score / strategy.
    # Check your logs for eval= values; anything above ~20 ms per symbol
    # is worth investigating inside SignalEngine itself.

    async def _scan(
        self,
        symbol: str,
        precomputed_stake: float = None,
    ) -> Optional[ScanResult]:
        t0 = time.perf_counter()
        try:
            ltf = self._ltf.get(symbol)
            htf = self._htf.get(symbol)
            if not ltf or not htf:
                return None
            if ltf.count < 3:
                return None

            current_price = float(
                ltf.completed_bars[-1].close
                if ltf.completed_bars else 0)
            if current_price == 0:
                return None

            # ── FIX 1: reuse stake computed once per cycle (avoid N awaits) ───
            t_stake0 = time.perf_counter()
            if precomputed_stake is not None:
                stake = precomputed_stake
            else:
                stake = await self.risk.calculate_stake()
            t_stake = (time.perf_counter() - t_stake0) * 1000

            # Safely get extra timeframes if they exist
            def safe_bars(store, sym):
                try:
                    b = store.get(sym)
                    return b.completed_bars if b else None
                except Exception:
                    return None

            # ── FIX 2: run CPU-bound evaluate() off the event loop ────────────
            loop = asyncio.get_event_loop()
            t_eval0 = time.perf_counter()
            sig = await loop.run_in_executor(
                None,  # default ThreadPoolExecutor
                lambda: self.signal.evaluate(
                    ltf_bars = ltf.completed_bars,
                    symbol   = symbol,
                    stake    = stake,
                    d1_bars  = safe_bars(getattr(self, "_d1",  {}), symbol),
                    h4_bars  = safe_bars(getattr(self, "_h4",  {}), symbol),
                    h1_bars  = safe_bars(getattr(self, "_h1",  {}), symbol),
                    m30_bars = safe_bars(getattr(self, "_m30", {}), symbol),
                    m15_bars = safe_bars(getattr(self, "_m15", {}), symbol),
                )
            )
            t_eval  = (time.perf_counter() - t_eval0) * 1000
            t_total = (time.perf_counter() - t0) * 1000

            logger.debug(
                f"SCAN_TIMING {symbol} "
                f"total={t_total:.1f}ms "
                f"eval={t_eval:.1f}ms "
                f"stake={t_stake:.1f}ms")

            if sig is None or sig.direction == "NONE":
                return None

            logger.info(
                f"SCAN HIT: {symbol} "
                f"{sig.direction} "
                f"score={sig.score:.3f} "
                f"strategy={sig.strategy} "
                f"[eval={t_eval:.1f}ms]")

            return ScanResult(
                symbol  = symbol,
                sig     = sig,
                price   = current_price,
                smc_ctx = None,
                score   = sig.score,
            )

        except Exception as e:
            logger.error(f"SCAN ERROR {symbol}: {e}")
            return None

    # ── Execution ──────────────────────────────────────────────────────────────

    async def _execute(
        self,
        symbol:  str,
        sig:     SignalResult,
        price:   float    = 0.0,
        smc_ctx: SMCContext = None,
    ) -> bool:
        if not self.symbols.can_trade_now(symbol):
            return False
        if symbol in self._active_symbols:
            return False
        if not self.risk.can_trade():
            return False

        stake     = await self.risk.calculate_stake()
        direction = sig.direction
        ac        = get_symbol_class(symbol)

        if smc_ctx is None:
            smc_ctx = SMCContext()

        # ── Record signal on every trade OPEN ─────────────────────────────────
        record_signal(
            symbol    = symbol,
            direction = sig.direction,
            strategy  = getattr(sig, "strategy", "unknown"),
            score     = getattr(sig, "score",    0.0),
        )

        buy_resp = await self.client.buy_contract(
            symbol      = symbol,
            direction   = direction,
            stake       = stake,
            multiplier  = sig.multiplier,
            stop_loss   = sig.stop_loss,
            take_profit = sig.take_profit,
        )

        if buy_resp is None:
            logger.warning(f"PLACEMENT FAILED: {symbol}")
            # ── Record failure ─────────────────────────────────────────────────
            record_failure(
                symbol    = symbol,
                direction = direction,
                stake     = stake,
                strategy  = getattr(sig, "strategy", "unknown"),
                reason    = "buy_resp=None",
            )
            try:
                self._push_dashboard()
            except Exception:
                pass
            return False

        cid       = str(buy_resp.get("contract_id", ""))
        bal_b     = self.client.balance
        buy_price = float(buy_resp.get("buy_price", stake))

        rec = self.risk.register_open(
            symbol      = symbol,
            direction   = direction,
            stake       = stake,
            entry_price = buy_price,
        )

        self.journal.open_trade(
            contract_id    = cid,
            symbol         = symbol,
            direction      = direction,
            stake          = stake,
            entry_price    = buy_price,
            balance_before = bal_b,
            asset_class    = ac,
            htf_bias       = getattr(smc_ctx, "bias",      "NEUTRAL"),
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
            "entry_price": buy_price,
            "opened_at":   time.time(),
            "rec":         rec,
            "sig":         sig,
            "strategy":    getattr(sig, "strategy",    "unknown"),
            "stop_loss":   getattr(sig, "stop_loss",   None),
            "take_profit": getattr(sig, "take_profit", None),
            "multiplier":  getattr(sig, "multiplier",  None),
        }

        self.symbols.record_trade_placed(symbol)
        self._active_symbols.add(symbol)
        set_active_trades(len(self._open_contracts))

        logger.info(
            f"▶ {direction} {symbol} | ${stake:.2f} | "
            f"x{getattr(sig, 'multiplier', '?')} | "
            f"SL={getattr(sig, 'stop_loss', '?')} "
            f"TP={getattr(sig, 'take_profit', '?')} | "
            f"modules={sig.strength}/3 | streak={self.risk.current_streak}")

        await self.client.subscribe_contract(
            cid,
            lambda msg, _cid=cid: asyncio.create_task(
                self._on_contract_result(_cid, msg)))

        # ── Push dashboard after trade OPENS ──────────────────────────────────
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
        won        = pnl > 0

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

        logger.info(
            f"{'WIN' if won else 'LOSS'}: {symbol} pnl=${pnl:+.4f} sell_price={sell_price}")

        self.journal.close_trade(
            contract_id   = cid,
            exit_price    = sell_price,
            pnl           = pnl,
            payout        = payout,
            balance_after = bal_after,
        )

        # ── Record trade on every normal CLOSE ───────────────────────────────
        record_trade(
            symbol        = symbol,
            direction     = direction,
            stake         = stake,
            pnl           = pnl,
            balance_after = self.client.balance,
            won           = pnl > 0,
            strategy      = info.get("strategy", ""),
            multiplier    = info.get("multiplier", None),
            close_reason  = "normal",
        )

        self._active_symbols.discard(symbol)
        self.symbols.record_contract_closed(symbol)
        # Force queue update
        self.symbols.update_active(list(self._htf.keys()))
        logger.info(f"SYMBOL RELEASED: {symbol} | Active now: {self._active_symbols}")
        self.symbols.record_result(symbol, won=won)
        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)
        self._confirmed_daily_loss += abs(pnl) if pnl < 0 else 0
        self._check_confirmed_loss_limit()
        set_active_trades(len(self._open_contracts))
        update_status(streak=self.risk.current_streak)

        # ── Push dashboard after trade CLOSES ─────────────────────────────────
        self._push_dashboard()

        logger.info(
            f"{'✅ WIN' if pnl > 0 else '❌ LOSS'} | "
            f"{symbol} | pnl=${pnl:+.4f} | "
            f"balance=${self.client.balance:.4f} | "
            f"streak={self.risk.current_streak}")

    # ── Orphan monitoring ──────────────────────────────────────────────────────

    async def _monitor_orphans(self):
        try:
            now = time.time()
            for cid in list(self._open_contracts.keys()):
                info = self._open_contracts.get(cid)
                if not info:
                    continue
                open_time = info.get("opened_at", now)
                age       = now - open_time

                if age >= 1800:
                    symbol = info["symbol"]
                    stake  = info["stake"]
                    rec    = info["rec"]

                    self._open_contracts.pop(cid, None)
                    self._contract_open_times.pop(cid, None)
                    self._active_symbols.discard(symbol)

                    self.symbols.record_result(symbol, won=False)
                    self.risk.register_close(rec, exit_price=0, pnl=-stake)
                    self._confirmed_daily_loss += stake
                    self._check_confirmed_loss_limit()
                    set_active_trades(len(self._open_contracts))

                    # ── Genuine orphan after 30min — record as loss ───────────
                    record_trade(
                        symbol        = symbol,
                        direction     = info.get("direction", "?"),
                        stake         = stake,
                        pnl           = -stake,
                        balance_after = self.client.balance,
                        won           = False,
                        strategy      = "ORPHAN_TIMEOUT",
                        multiplier    = info.get("multiplier", None),
                        close_reason  = "timeout",
                    )

                    logger.error(
                        f"GENUINE ORPHAN: {cid} recording as loss after 30min "
                        f"({age:.0f}s)")
                    try:
                        self._push_dashboard()
                    except Exception:
                        pass

                elif age >= CONTRACT_FORCE_CLOSE_SECS:
                    logger.warning(
                        f"CONTRACT STALE: {cid} {info['symbol']} "
                        f"age={age/60:.1f}min — poller will resolve")
                    try:
                        await self.client.force_check_contract(cid)
                    except Exception as exc:
                        logger.warning(f"force_check_contract({cid}): {exc}")

                elif age >= CONTRACT_MAX_AGE_SECS:
                    try:
                        await self.client.force_check_contract(cid)
                        logger.debug(
                            f"force_check_contract({cid}) triggered "
                            f"(age={age:.0f}s)")
                    except Exception as exc:
                        logger.warning(f"force_check_contract({cid}): {exc}")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"_monitor_orphans: {exc}")

    # ── Settle task launcher ───────────────────────────────────────────────────

    async def _settle_and_resolve(self, wait_secs: float):
        try:
            await asyncio.sleep(wait_secs)
            await self._resolve_remaining_contracts()
            self._cycle_count += 1
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"_settle_and_resolve: {exc}")

    # ── Orphan resolution (full) ───────────────────────────────────────────────

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
                stake  = info.get("stake",  0.0)
                rec    = info.get("rec",    None)

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

                # ── Record resolution orphan as TIMEOUT loss ──────────────────
                record_trade(
                    symbol        = symbol,
                    direction     = info.get("direction", "?"),
                    stake         = stake,
                    pnl           = -stake,
                    balance_after = self.client.balance,
                    won           = False,
                    strategy      = "RESOLVE_TIMEOUT",
                    multiplier    = info.get("multiplier", None),
                    close_reason  = "timeout",
                )

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

    # ── Contract watchdog ──────────────────────────────────────────────────────

    async def _contract_watchdog(self):
        max_open_secs = getattr(config, "MAX_TRADE_OPEN_MINS", 60) * 60

        while True:
            try:
                await asyncio.sleep(15)
                now = time.time()
                for cid, info in list(self._open_contracts.items()):
                    age_secs = now - info.get("opened_at", now)
                    age_mins = age_secs / 60

                    if age_secs >= config.CONTRACT_CHECK_SECS:
                        logger.info(
                            f"WATCHDOG CHECK: {cid} "
                            f"{info['symbol']} age={age_mins:.1f}min")
                        try:
                            result = await self.client.force_check_contract(cid)
                            if result.get("is_sold") or result.get("is_expired"):
                                await self._on_contract_result(
                                    cid, {"proposal_open_contract": result})
                        except Exception as exc:
                            logger.warning(f"WATCHDOG force_check({cid}): {exc}")

                    # Funding-fee timeout — force-close to prevent fee erosion
                    if age_secs >= max_open_secs:
                        if cid in self._open_contracts:
                            stake  = info["stake"]
                            symbol = info["symbol"]
                            logger.warning(
                                f"FUNDING TIMEOUT: {cid} {symbol} "
                                f"{age_mins:.1f}min >= MAX_TRADE_OPEN_MINS="
                                f"{getattr(config,'MAX_TRADE_OPEN_MINS',60)} — "
                                f"force-closing to prevent fee erosion")
                            try:
                                await self.client.sell_contract(cid)
                            except Exception as exc:
                                logger.warning(
                                    f"sell_contract({cid}): {exc} — "
                                    f"recording as loss")
                            # ── Record FUNDING TIMEOUT close ──────────────────
                            record_trade(
                                symbol        = symbol,
                                direction     = info.get("direction", "?"),
                                stake         = stake,
                                pnl           = -stake,
                                balance_after = self.client.balance,
                                won           = False,
                                strategy      = "FUNDING_TIMEOUT",
                                multiplier    = info.get("multiplier", None),
                                close_reason  = "timeout",
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
                            # ── Push after FUNDING TIMEOUT ─────────────────────
                            self._push_dashboard()

                    # Legacy hard timeout (CONTRACT_TIMEOUT_SECS)
                    elif age_secs >= config.CONTRACT_TIMEOUT_SECS:
                        if cid in self._open_contracts:
                            stake  = info["stake"]
                            symbol = info["symbol"]

                            if age_secs >= 1800:
                                logger.error(
                                    f"GENUINE ORPHAN: {cid} {symbol} "
                                    f"recording as loss after 30min "
                                    f"({age_mins:.1f}min)")
                                # ── Record genuine orphan TIMEOUT close ────────
                                record_trade(
                                    symbol        = symbol,
                                    direction     = info.get("direction", "?"),
                                    stake         = stake,
                                    pnl           = -stake,
                                    balance_after = self.client.balance,
                                    won           = False,
                                    strategy      = "TIMEOUT",
                                    multiplier    = info.get("multiplier", None),
                                    close_reason  = "timeout",
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
                                # ── Push after genuine orphan TIMEOUT ──────────
                                self._push_dashboard()
                            else:
                                logger.warning(
                                    f"CONTRACT STALE: {cid} {symbol} "
                                    f"age={age_mins:.1f}min — poller will resolve")
                                # Do NOT record as loss here — let polling
                                # get the real result via force_check_contract.

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"_contract_watchdog: {exc}")

    # ── Symbol release watchdog ─────────────────────────────────────────────────

    async def _symbol_release_watchdog(self):
        """
        Every 60 seconds, force-release any symbol from _active_symbols
        whose contract has been open longer than 8 minutes (480s). This
        guarantees the queue can never permanently empty because a symbol
        is stuck "active" due to a missed close callback.
        """
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                released = []
                for cid, info in list(self._open_contracts.items()):
                    age = now - info.get("opened_at", now)
                    if age > 480:  # 8 minutes
                        symbol = info["symbol"]
                        logger.warning(
                            f"FORCE RELEASING: {symbol} age={age:.0f}s")
                        self._active_symbols.discard(symbol)
                        self.symbols.record_contract_closed(symbol)
                        released.append(symbol)
                if released:
                    logger.info(
                        f"RELEASED SYMBOLS: {released} | "
                        f"Queue should refill now")
                    self.symbols.update_active(list(self._htf.keys()))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"_symbol_release_watchdog: {exc}")

    # ── Stale contract detection ───────────────────────────────────────────────


    def _has_stale_contracts(self) -> bool:
        stale_threshold = config.TRADE_DURATION * 60 + 30
        now = time.time()
        for cid, open_time in list(self._contract_open_times.items()):
            if cid in self._open_contracts:
                if now - open_time > stale_threshold:
                    return True
        return False
