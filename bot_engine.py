"""
bot_engine.py – Central async orchestrator (parallel + HCM edition).

What changed in this revision
------------------------------
1.  High Confidence Mode (HCM)
    The "startup advantage" — the observation that the first few trades of
    each session are almost always wins — is now formalised as HCM and runs
    permanently under two conditions:

        a) First HCM_DAILY_TRADE_COUNT completed trades of the UTC day.
        b) After HCM_LOSS_TRIGGER consecutive losses (recovery mode).

    In HCM:
        • All 3 signal modules must confirm (not just 2).
        • The same indicator vote threshold applies (5/7).
        • Only the single top-ranked signal fires per cycle.

2.  Signal freshness guard
    _last_traded_bar[symbol] records the LTF bar epoch at which the last
    trade for that symbol was executed.  A new trade is blocked until at
    least MIN_BARS_BETWEEN_SAME_SYMBOL fresh bars have closed, preventing
    the bot from re-entering the exact same failing setup repeatedly.

3.  Adaptive quality thresholds
    _get_quality_thresholds() returns (min_modules, min_votes) appropriate
    for the current mode (HCM or standard), injected into signal.evaluate().

4.  Parallel scanning (retained from previous revision)
    All initialised symbols are evaluated simultaneously via asyncio.gather.
    Signals are ranked by composite probability score; the highest-conviction
    opportunity is executed first, up to MAX_CONCURRENT_TRADES slots.

5.  Per-class LTF granularity (retained from previous revision)
    Forex (frx*): 15-min LTF.   Synthetics / crypto: 1-min LTF.

6.  90 % daily loss limit (retained from previous revision)
    Pause until UTC midnight when balance falls to ≤ 10 % of day start.
"""

import asyncio
import logging
import time
import traceback
import datetime
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

import config
import symbols as sym_module
from deriv_client import DerivClient
from candlestick_builder import CandlestickBuilder
from smc_analyzer import SMCAnalyzer
from signal_engine import SignalEngine, SignalResult
from risk_manager import RiskManager
from news_filter import NewsFilter
from trade_journal import TradeJournal
from symbol_manager import SymbolManager
import indicators as ind
from keep_alive import update_status
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

SCAN_INTERVAL         = config.SCAN_INTERVAL
SYMBOL_REFRESH_EVERY  = 3600
DASHBOARD_PUSH_EVERY  = 15
BALANCE_REFRESH_EVERY = 60


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_forex(symbol: str) -> bool:
    return symbol.startswith("frx")

def _ltf_granularity(symbol: str) -> int:
    return (config.LTF_GRANULARITY_FOREX
            if _is_forex(symbol)
            else config.LTF_GRANULARITY_SYNTHETIC)

def _ltf_bars(symbol: str) -> int:
    return (config.LTF_BARS_FOREX
            if _is_forex(symbol)
            else config.LTF_BARS_SYNTHETIC)


# ─── BotEngine ────────────────────────────────────────────────────────────────

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

        # Per-symbol OHLCV stores
        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}

        # Init tracking
        self._initializing: Set[str] = set()
        self._init_sem = asyncio.Semaphore(config.INIT_BATCH_SIZE)

        # Open contracts: contract_id → (symbol, direction, stake, entry, rec)
        self._open_contracts: dict = {}

        # Symbol queue
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])

        # ── HCM & session state ────────────────────────────────────────────
        # Number of trades completed in the current UTC day.
        # While this is < HCM_DAILY_TRADE_COUNT the bot runs in HCM.
        self._daily_trades: int = 0

        # Consecutive losses since the last win.
        # Resets to 0 on each win; when it reaches HCM_LOSS_TRIGGER the bot
        # re-enters HCM until the next win resets it again.
        self._consecutive_losses: int = 0

        # Tracks which UTC day the counters belong to so they auto-reset.
        self._current_day: str = ""

        # Signal freshness: symbol → LTF bar epoch of last executed trade.
        # Prevents re-entering the same setup on consecutive bars.
        self._last_traded_bar: Dict[str, int] = {}

        self._last_symbol_refresh = 0.0
        self._last_dashboard_push = 0.0

    # ─── Mode helpers ─────────────────────────────────────────────────────────

    @property
    def _in_hcm(self) -> bool:
        """True when High Confidence Mode is active."""
        startup_window   = self._daily_trades < config.HCM_DAILY_TRADE_COUNT
        recovery_mode    = self._consecutive_losses >= config.HCM_LOSS_TRIGGER
        return startup_window or recovery_mode

    def _get_quality_thresholds(self) -> Tuple[int, int]:
        """Return (min_modules, min_votes) for the current mode."""
        if self._in_hcm:
            return config.HCM_MIN_MODULES, config.HCM_MIN_VOTES
        return config.MIN_MODULES_FOR_SIGNAL, config.MIN_INDICATOR_VOTES

    def _max_executions_this_cycle(self) -> int:
        """In HCM only the single best signal fires; otherwise fill all slots."""
        return config.HCM_MAX_EXECUTE if self._in_hcm else config.MAX_CONCURRENT_TRADES

    def _check_day_rollover(self):
        """Reset daily counters at UTC midnight."""
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._current_day:
            self._current_day        = today
            self._daily_trades       = 0
            self._consecutive_losses = 0
            logger.info(
                f"UTC day rolled over → {today} | "
                f"HCM active (first {config.HCM_DAILY_TRADE_COUNT} trades)"
            )

    # ─── Entry point ──────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Bot  –  parallel + HCM edition")
        logger.info("=" * 64)

        self._check_day_rollover()
        self.client.on_balance(self._on_balance)

        ws_task = asyncio.create_task(self.client.connect())
        for _ in range(60):
            if self.client.is_connected:
                break
            await asyncio.sleep(1)

        if not self.client.is_connected:
            logger.error("Could not connect/authorise within 60 s")
            ws_task.cancel()
            return

        self.risk.set_balance(self.client.balance)
        logger.info(
            f"Starting balance: ${self.client.balance:.4f} | "
            f"HCM active: {self._in_hcm}"
        )

        await self._refresh_symbols()

        dash_task    = asyncio.create_task(self._dashboard_loop())
        balance_task = asyncio.create_task(
            self.client.balance_refresh_loop(BALANCE_REFRESH_EVERY))

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.critical(f"Main loop crashed: {exc}\n{traceback.format_exc()}")
        finally:
            dash_task.cancel()
            balance_task.cancel()
            ws_task.cancel()

    # ─── Balance callback ─────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)
        logger.info(f"Balance sync: ${balance:.4f}")

    # ─── Symbol discovery ─────────────────────────────────────────────────────

    async def _refresh_symbols(self):
        active_raw = await self.client.get_active_symbols()
        active_syms = [
            s["symbol"] for s in active_raw
            if s.get("exchange_is_open", 0) == 1 or
               any(s["symbol"].startswith(p)
                   for p in ("R_", "1HZ", "BOOM", "CRASH", "JD",
                              "RDBEAR", "RDBULL", "stpRNG"))
        ]
        self.symbols.update_active(active_syms)
        self._queue = self.symbols.get_queue(
            max_symbols=config.MAX_SYMBOLS_PER_QUEUE)
        self._last_symbol_refresh = time.time()
        logger.info(
            f"Symbol queue refreshed: {len(self._queue)} instruments | "
            f"session: {self.symbols.current_session}"
        )

    # ─── Parallel initialisation ──────────────────────────────────────────────

    async def _ensure_all_initialized(self):
        pending = [s for s in self._queue
                   if s not in self._htf and s not in self._initializing]
        if not pending:
            return
        logger.info(f"Initialising {len(pending)} symbol(s) …")
        results = await asyncio.gather(
            *[self._init_data(s) for s in pending],
            return_exceptions=True,
        )
        for sym, res in zip(pending, results):
            if isinstance(res, Exception):
                logger.error(f"Init failed [{sym}]: {res}")

    # ─── Dashboard ────────────────────────────────────────────────────────────

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
        mode    = "HCM 🔒" if self._in_hcm else "Standard"
        update_status(
            running               = True,
            mode                  = mode,
            balance               = self.client.balance,
            day_start_balance     = self.risk.day_start_balance,
            paused_for_loss_limit = self.risk.is_paused,
            daily_trades          = self._daily_trades,
            consecutive_losses    = self._consecutive_losses,
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
            streak                = summary.get("streak", 0),
            recent_trades         = self.journal.recent_trades(20),
            best_symbols          = self.symbols.best_symbols(10),
        )

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def _main_loop(self):
        """
        Each cycle:
          0. Check day rollover → reset daily counters if needed.
          1. Refresh symbol list hourly.
          2. Skip if trading is paused (90 % daily loss limit).
          3. Ensure all queued symbols have historical data loaded.
          4. Evaluate all initialised symbols in parallel.
          5. Rank signals by composite probability score.
          6. Execute top signal(s) respecting HCM limits and concurrency cap.
          7. Sleep for the remainder of SCAN_INTERVAL.
        """
        while True:
            cycle_start = time.time()

            # 0. Day rollover check
            self._check_day_rollover()

            # 1. Hourly symbol refresh
            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()

            # 2. Loss-limit pause
            if self.risk.is_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(
                    f"⛔ Trading paused (90 % daily loss) | "
                    f"resumes in ~{mins:.0f} min | "
                    f"balance=${self.client.balance:.4f}"
                )
                await asyncio.sleep(60)
                continue

            if not self._queue:
                logger.warning("Empty symbol queue – refreshing in 30 s")
                await asyncio.sleep(30)
                await self._refresh_symbols()
                continue

            # 3. Initialise any new symbols
            await self._ensure_all_initialized()

            # 4. Parallel signal scan
            min_mod, min_vot = self._get_quality_thresholds()
            candidates       = await self._scan_all_parallel(min_mod, min_vot)

            # 5. Rank by composite probability score (highest first)
            candidates.sort(key=lambda x: x[4], reverse=True)

            mode_label = "HCM 🔒" if self._in_hcm else "Standard"
            if candidates:
                top = candidates[0]
                logger.info(
                    f"[{mode_label}] 🏆 Best signal: [{top[0]}] "
                    f"{top[1].direction} | score={top[4]:.4f} | {top[1].reason}"
                )
            else:
                logger.debug(f"[{mode_label}] No qualifying signals this cycle")

            # 6. Execute — HCM limits to 1, standard fills concurrency slots
            max_exec  = self._max_executions_this_cycle()
            executed  = 0
            for sym, sig, price, smc_ctx, score in candidates:
                if executed >= max_exec:
                    break
                if not self.risk.can_trade():
                    break
                update_status(
                    current_symbol = sym,
                    last_signal    = (
                        f"[{sym}] {sig.direction} | "
                        f"score={score:.4f} | {sig.reason}"
                    ),
                )
                await self._execute(sym, sig, price, smc_ctx, score)
                executed += 1

            if candidates and executed == 0:
                logger.debug(
                    f"Signals found but blocked "
                    f"(open={self.risk._open_trade_count}/"
                    f"{self.risk.max_concurrent})"
                )

            # 7. Sleep for remainder of cycle
            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0.1, SCAN_INTERVAL - elapsed))

    # ─── Parallel signal collection ───────────────────────────────────────────

    async def _scan_all_parallel(
        self, min_modules: int, min_votes: int
    ) -> List[Tuple]:
        """
        Evaluate every initialised symbol concurrently.
        Returns list of (symbol, SignalResult, price, smc_ctx, composite_score).
        """
        ready = [s for s in self._queue if s in self._htf]
        if not ready:
            return []

        results = await asyncio.gather(
            *[self._scan_for_signal(s, min_modules, min_votes) for s in ready],
            return_exceptions=True,
        )

        valid = []
        for sym, res in zip(ready, results):
            if isinstance(res, Exception):
                logger.debug(f"Scan error [{sym}]: {res}")
            elif res is not None:
                valid.append(res)
        return valid

    # ─── Single-symbol signal evaluation ─────────────────────────────────────

    async def _scan_for_signal(
        self,
        symbol:      str,
        min_modules: int,
        min_votes:   int,
    ) -> Optional[Tuple]:
        """
        Run the full SIFM pipeline for one symbol.

        Pure evaluation — no trade is executed here.
        Returns (symbol, sig, price, smc_ctx, composite_score) or None.
        """
        htf = self._htf.get(symbol)
        ltf = self._ltf.get(symbol)
        if htf is None or ltf is None:
            return None
        if htf.count < 20 or ltf.count < 30:
            return None

        # ── Signal freshness guard ─────────────────────────────────────────
        ltf_bars_list = ltf.completed_bars
        if not ltf_bars_list:
            return None
        current_bar_epoch = ltf_bars_list[-1].timestamp
        last_epoch        = self._last_traded_bar.get(symbol, 0)
        gran_ltf          = _ltf_granularity(symbol)
        min_gap_seconds   = config.MIN_BARS_BETWEEN_SAME_SYMBOL * gran_ltf
        if current_bar_epoch < last_epoch + min_gap_seconds:
            logger.debug(
                f"[{symbol}] Freshness guard: waiting for "
                f"{config.MIN_BARS_BETWEEN_SAME_SYMBOL} bar gap"
            )
            return None

        # ── Phase A: HTF SMC structure ─────────────────────────────────────
        htf_bars    = htf.completed_bars
        H           = np.array([b.high  for b in htf_bars])
        L           = np.array([b.low   for b in htf_bars])
        C           = np.array([b.close for b in htf_bars])
        htf_atr_arr = ind.atr(H, L, C, 14)
        valid_ha    = htf_atr_arr[~np.isnan(htf_atr_arr)]
        htf_atr     = float(valid_ha[-1]) if len(valid_ha) else 0.0

        smc_ctx = self.smc.analyse(htf_bars, htf_atr)
        if smc_ctx.bias == "NEUTRAL":
            return None

        # ── Phase B.1: Price in SMC zone? ─────────────────────────────────
        current_price = float(ltf_bars_list[-1].close)
        in_zone = self.smc.price_in_smc_zone(
            current_price, smc_ctx.bias, smc_ctx)
        if not in_zone:
            return None

        # ── Phase B.2-B.3: Signal modules (thresholds injected) ───────────
        sig = self.signal.evaluate(
            ltf_bars    = ltf_bars_list,
            htf_bias    = smc_ctx.bias,
            smc_ctx     = smc_ctx,
            in_zone     = in_zone,
            min_modules = min_modules,
            min_votes   = min_votes,
        )
        if sig.direction == "NONE":
            return None

        # ── Filters ────────────────────────────────────────────────────────
        if self.news.is_blocked(symbol):
            logger.debug(f"[{symbol}] News block — skipped")
            return None

        # Volatility filter: LTF ATR must not exceed 2× HTF ATR
        ltf_H = np.array([b.high  for b in ltf_bars_list])
        ltf_L = np.array([b.low   for b in ltf_bars_list])
        ltf_C = np.array([b.close for b in ltf_bars_list])
        ltf_atr_arr = ind.atr(ltf_H, ltf_L, ltf_C, 14)
        valid_la    = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
        ltf_atr     = float(valid_la[-1]) if len(valid_la) else 0.0
        if htf_atr > 0 and ltf_atr > 2 * htf_atr:
            logger.debug(
                f"[{symbol}] Volatility filter — ltf_atr={ltf_atr:.5f} "
                f"> 2×htf_atr={htf_atr:.5f}"
            )
            return None

        # ── Composite probability score ────────────────────────────────────
        # 70% from signal engine (module agreement + vote density)
        # 30% from SMC trend conviction
        trend_score = min(getattr(smc_ctx, "trend_strength", 0.5), 1.0)
        composite   = round(sig.probability_score * 0.70 + trend_score * 0.30, 4)

        logger.debug(
            f"[{symbol}] {sig.direction} | "
            f"base={sig.probability_score:.4f} "
            f"trend={trend_score:.4f} composite={composite:.4f}"
        )
        return (symbol, sig, current_price, smc_ctx, composite)

    # ─── Execution ────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult,
                       price: float, smc_ctx, score: float = 0.0):
        stake = self.risk.calculate_stake()
        ac    = get_symbol_class(symbol)
        mode  = "HCM" if self._in_hcm else "STD"

        logger.info(
            f"▶ [{mode}] {sig.direction} {symbol} | ${stake:.2f} | "
            f"score={score:.4f} | modules={sig.strength}/3 | "
            f"balance=${self.client.balance:.4f}"
        )

        buy_resp = await self.client.buy_contract(
            symbol    = symbol,
            direction = sig.direction,
            stake     = stake,
            duration  = config.TRADE_DURATION,
            dur_unit  = config.TRADE_DURATION_UNIT,
        )
        if not buy_resp:
            logger.warning(f"Order rejected for {symbol}")
            return

        cid   = str(buy_resp.get("contract_id", ""))
        bal_b = self.client.balance

        rec = self.risk.register_open(
            symbol      = symbol,
            direction   = sig.direction,
            stake       = stake,
            entry_price = price,
        )

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

        # Mark this bar as traded for the freshness guard
        ltf = self._ltf.get(symbol)
        if ltf and ltf.completed_bars:
            self._last_traded_bar[symbol] = ltf.completed_bars[-1].timestamp

        await self.client.subscribe_contract(
            cid,
            lambda msg, _cid=cid: asyncio.create_task(
                self._on_contract_result(_cid, msg)
            ),
        )

    # ─── Contract result callback ─────────────────────────────────────────────

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
        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)

        self.journal.close_trade(
            contract_id   = cid,
            exit_price    = sell_price,
            pnl           = pnl,
            payout        = payout,
            balance_after = bal_after,
        )

        self.symbols.record_trade(symbol, won=won, pnl=pnl)

        # ── Update HCM counters ────────────────────────────────────────────
        self._daily_trades += 1

        if won:
            prev_losses = self._consecutive_losses
            self._consecutive_losses = 0
            if prev_losses >= config.HCM_LOSS_TRIGGER:
                logger.info(
                    f"✅ WIN after {prev_losses} consecutive losses — "
                    f"exiting recovery HCM"
                )
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= config.HCM_LOSS_TRIGGER:
                logger.warning(
                    f"❌ {self._consecutive_losses} consecutive losses — "
                    f"HCM (recovery mode) activated"
                )

        still_hcm  = self._in_hcm
        mode_label = "HCM 🔒" if still_hcm else "Standard"
        outcome    = "✅ WIN" if won else "❌ LOSS"

        logger.info(
            f"{outcome} | {symbol} | dir={direction} | "
            f"pnl=${pnl:+.4f} | stake=${stake:.2f} | "
            f"balance=${bal_after:.4f} | "
            f"daily_trades={self._daily_trades} | "
            f"consec_losses={self._consecutive_losses} | "
            f"mode={mode_label}"
        )

    # ─── Data initialisation ──────────────────────────────────────────────────

    async def _init_data(self, symbol: str):
        """
        Load historical OHLCV and subscribe to live ticks for one symbol.
        Semaphore-controlled to cap concurrent WebSocket requests.
        """
        if symbol in self._initializing or symbol in self._htf:
            return
        self._initializing.add(symbol)

        async with self._init_sem:
            try:
                gran_ltf = _ltf_granularity(symbol)
                bars_ltf = _ltf_bars(symbol)

                htf_b = CandlestickBuilder(
                    granularity = config.HTF_GRANULARITY,
                    max_bars    = config.HTF_BARS + 20,
                )
                ltf_b = CandlestickBuilder(
                    granularity = gran_ltf,
                    max_bars    = bars_ltf + 20,
                )

                htf_data, ltf_data = await asyncio.gather(
                    self.client.get_candles(
                        symbol, config.HTF_GRANULARITY, config.HTF_BARS),
                    self.client.get_candles(symbol, gran_ltf, bars_ltf),
                    return_exceptions=True,
                )

                if isinstance(htf_data, Exception): htf_data = []
                if isinstance(ltf_data, Exception): ltf_data = []

                if not htf_data and not ltf_data:
                    logger.warning(f"No data for {symbol} — skipping")
                    return

                if htf_data: htf_b.seed(htf_data)
                if ltf_data: ltf_b.seed(ltf_data)

                self._htf[symbol] = htf_b
                self._ltf[symbol] = ltf_b

                await self.client.subscribe_ticks(
                    symbol,
                    lambda tick, s=symbol: self._on_tick(s, tick),
                )

                ltf_label = "15m" if _is_forex(symbol) else "1m"
                logger.info(
                    f"{symbol}: ready | htf={htf_b.count}×1h | "
                    f"ltf={ltf_b.count}×{ltf_label}"
                )

            except Exception as exc:
                logger.error(
                    f"_init_data failed [{symbol}]: {exc}\n"
                    f"{traceback.format_exc()}"
                )
            finally:
                self._initializing.discard(symbol)

    # ─── Tick handler ─────────────────────────────────────────────────────────

    def _on_tick(self, symbol: str, tick: dict):
        import time as _t
        epoch = int(tick.get("epoch", _t.time()))
        price = float(tick.get("quote", 0))
        if price == 0:
            return
        if symbol in self._ltf:
            self._ltf[symbol].add_tick(epoch, price)
        if symbol in self._htf:
            self._htf[symbol].add_tick(epoch, price)
