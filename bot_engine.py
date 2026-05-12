"""
bot_engine.py – Central async orchestrator (parallel scanning edition).

Architecture change vs. the sequential version
------------------------------------------------
OLD: One symbol scanned every SCAN_INTERVAL seconds (round-robin queue).
     First valid signal found is executed immediately.

NEW: Every SCAN_INTERVAL seconds ALL initialised symbols are evaluated
     simultaneously via asyncio.gather().  The resulting signals are ranked
     by a composite probability score (module agreement + SMC trend strength +
     indicator vote density).  The top-scoring signal(s) are executed — up to
     MAX_CONCURRENT_TRADES slots — so the bot always trades the highest-
     conviction opportunity available across the entire symbol universe.

LTF granularity is now per-instrument-class:
  • Forex pairs  (frx* prefix)  → 15-min (900 s) LTF
  • Synthetics / Crypto / rest  → 1-min  (60 s)  LTF

The 90 % daily loss limit is enforced by RiskManager; once triggered,
the main loop sleeps until UTC midnight before attempting new trades.

Integrates:
  • DerivClient        – WebSocket connection, ticks, trades
  • CandlestickBuilder – tick → OHLCV per symbol (HTF + LTF)
  • SMCAnalyzer        – Phase A: HTF bias + SMC zones
  • SignalEngine       – Phase B: M1 / M2 / M3 modules
  • RiskManager        – Phase C: 90 % daily limit + 1 % compounding stake
  • NewsFilter         – calendar-based trade blackout
  • TradeJournal       – persistent trade log + analytics
  • SymbolManager      – adaptive priority queue + session awareness
  • keep_alive         – feeds live data to the dashboard
"""

import asyncio
import logging
import time
import traceback
from typing import Dict, List, Optional, Tuple, Set

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

# ─── Timing constants ─────────────────────────────────────────────────────────
SCAN_INTERVAL         = config.SCAN_INTERVAL          # seconds between full parallel scans
SYMBOL_REFRESH_EVERY  = 3600                           # re-fetch active symbols list hourly
DASHBOARD_PUSH_EVERY  = 15                             # push dashboard every N seconds
BALANCE_REFRESH_EVERY = 60                             # poll real balance every 60 s


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_forex(symbol: str) -> bool:
    """True for forex pairs that use a 15-min LTF."""
    return symbol.startswith("frx")


def _ltf_granularity(symbol: str) -> int:
    """Return the correct LTF bar width (seconds) for a given symbol."""
    return (config.LTF_GRANULARITY_FOREX
            if _is_forex(symbol)
            else config.LTF_GRANULARITY_SYNTHETIC)


def _ltf_bars(symbol: str) -> int:
    """Return the number of LTF history bars to seed for a given symbol."""
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

        # Tracks symbols currently being initialised so we don't double-init
        self._initializing: Set[str] = set()

        # Semaphore: limit concurrent symbol initialisations to avoid
        # flooding the Deriv WebSocket with simultaneous history requests
        self._init_sem = asyncio.Semaphore(config.INIT_BATCH_SIZE)

        # Active contracts: contract_id → (symbol, direction, stake, entry, rec)
        self._open_contracts: dict = {}

        # Symbol rotation queue
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])

        self._last_symbol_refresh  = 0.0
        self._last_dashboard_push  = 0.0

    # ─── Entry point ──────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Trading Bot  –  starting (parallel mode)")
        logger.info("=" * 64)

        self.client.on_balance(self._on_balance)

        ws_task = asyncio.create_task(self.client.connect())

        # Wait up to 60 s for connection + auth
        for _ in range(60):
            if self.client.is_connected:
                break
            await asyncio.sleep(1)

        if not self.client.is_connected:
            logger.error("Could not connect/authorise within 60 s")
            ws_task.cancel()
            return

        self.risk.set_balance(self.client.balance)
        logger.info(f"Starting balance: ${self.client.balance:.4f}")

        await self._refresh_symbols()

        dash_task    = asyncio.create_task(self._dashboard_loop())
        balance_task = asyncio.create_task(
            self.client.balance_refresh_loop(BALANCE_REFRESH_EVERY))

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.critical(
                f"Main loop crashed: {exc}\n{traceback.format_exc()}")
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

    # ─── Parallel symbol initialisation ───────────────────────────────────────

    async def _ensure_all_initialized(self):
        """
        Initialise any queued symbols that have not yet had their
        historical data loaded.  Runs in controlled batches so the
        WebSocket is not overwhelmed.
        """
        pending = [s for s in self._queue
                   if s not in self._htf and s not in self._initializing]
        if not pending:
            return

        logger.info(
            f"Initialising {len(pending)} new symbol(s) "
            f"(batch size={config.INIT_BATCH_SIZE}) …"
        )
        tasks = [self._init_data(sym) for sym in pending]

        # asyncio.gather respects the semaphore inside _init_data
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, res in zip(pending, results):
            if isinstance(res, Exception):
                logger.error(f"Init failed for {sym}: {res}")

    # ─── Dashboard push loop ──────────────────────────────────────────────────

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
            paused_for_loss_limit = self.risk.is_paused,
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
        1. Ensure all queued symbols have historical data loaded.
        2. Evaluate every initialised symbol in parallel.
        3. Sort resulting signals by probability score (descending).
        4. Execute the top signal(s) — as many as concurrent-trade slots allow.
        5. Sleep SCAN_INTERVAL and repeat.
        """
        while True:
            cycle_start = time.time()

            # ── Hourly symbol list refresh ─────────────────────────────────
            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()

            # ── Loss-limit pause ───────────────────────────────────────────
            if self.risk.is_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(
                    f"⛔ Trading paused (90 % daily loss limit reached) – "
                    f"resumes in ~{mins:.0f} min | "
                    f"balance=${self.client.balance:.4f}"
                )
                await asyncio.sleep(60)
                continue

            # ── Safety: empty queue ────────────────────────────────────────
            if not self._queue:
                logger.warning("Empty symbol queue – refreshing in 30 s")
                await asyncio.sleep(30)
                await self._refresh_symbols()
                continue

            # ── Phase 0: initialise any new symbols ────────────────────────
            await self._ensure_all_initialized()

            # ── Phase 1: scan ALL initialised symbols in parallel ──────────
            candidates = await self._scan_all_parallel()

            # ── Phase 2: rank by composite probability score ───────────────
            candidates.sort(key=lambda x: x[4], reverse=True)

            if candidates:
                top = candidates[0]
                logger.info(
                    f"🏆 Best signal this cycle: [{top[0]}] "
                    f"{top[1].direction} | score={top[4]:.4f} | {top[1].reason}"
                )

            # ── Phase 3: execute top signal(s) up to concurrent limit ──────
            executed = 0
            for sym, sig, price, smc_ctx, score in candidates:
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

            if not candidates:
                logger.debug("No qualifying signals this cycle")
            elif executed == 0:
                logger.debug(
                    f"{len(candidates)} signal(s) found but "
                    f"can_trade()=False "
                    f"(open={self.risk._open_trade_count}/"
                    f"{self.risk.max_concurrent})"
                )

            # ── Sleep for the remainder of SCAN_INTERVAL ───────────────────
            elapsed = time.time() - cycle_start
            sleep   = max(0.1, SCAN_INTERVAL - elapsed)
            await asyncio.sleep(sleep)

    # ─── Parallel signal collection ────────────────────────────────────────────

    async def _scan_all_parallel(self) -> List[Tuple]:
        """
        Evaluate every initialised symbol concurrently.

        Returns a list of tuples:
            (symbol, SignalResult, price, smc_ctx, composite_score)

        Only entries with a confirmed trade direction (not 'NONE') are included.
        """
        ready = [s for s in self._queue if s in self._htf]
        if not ready:
            return []

        tasks   = [self._scan_for_signal(sym) for sym in ready]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = []
        for sym, result in zip(ready, results):
            if isinstance(result, Exception):
                logger.debug(f"Scan exception [{sym}]: {result}")
            elif result is not None:
                valid.append(result)

        return valid

    # ─── Single-symbol signal evaluation (pure, no side effects) ──────────────

    async def _scan_for_signal(
        self, symbol: str
    ) -> Optional[Tuple]:
        """
        Run the full SIFM pipeline for one symbol.

        Returns (symbol, sig, price, smc_ctx, composite_score)
        or None if no tradeable signal exists.

        This method has NO side effects — it does not execute any trade.
        The main loop decides what to do with the result.
        """
        htf = self._htf.get(symbol)
        ltf = self._ltf.get(symbol)
        if htf is None or ltf is None:
            return None

        if htf.count < 20 or ltf.count < 30:
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
        ltf_bars_list = ltf.completed_bars
        if not ltf_bars_list:
            return None
        current_price = float(ltf_bars_list[-1].close)

        in_zone = self.smc.price_in_smc_zone(
            current_price, smc_ctx.bias, smc_ctx)
        if not in_zone:
            return None

        # ── Phase B.2-B.3: Signal modules ─────────────────────────────────
        sig = self.signal.evaluate(
            ltf_bars = ltf_bars_list,
            htf_bias = smc_ctx.bias,
            smc_ctx  = smc_ctx,
            in_zone  = in_zone,
        )

        if sig.direction == "NONE":
            return None

        # ── Filters ────────────────────────────────────────────────────────
        if self.news.is_blocked(symbol):
            logger.debug(f"News block: {symbol} skipped")
            return None

        # Volatility filter: LTF ATR must not exceed 2× HTF ATR
        ltf_H       = np.array([b.high  for b in ltf_bars_list])
        ltf_L       = np.array([b.low   for b in ltf_bars_list])
        ltf_C       = np.array([b.close for b in ltf_bars_list])
        ltf_atr_arr = ind.atr(ltf_H, ltf_L, ltf_C, 14)
        valid_la    = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
        ltf_atr     = float(valid_la[-1]) if len(valid_la) else 0.0
        if htf_atr > 0 and ltf_atr > 2 * htf_atr:
            logger.debug(
                f"Volatility filter: {symbol} skipped "
                f"(ltf_atr={ltf_atr:.5f} > 2×htf_atr={htf_atr:.5f})"
            )
            return None

        # ── Composite probability score ─────────────────────────────────────
        # Enrich the base signal score with SMC trend strength.
        # trend_strength is bounded to [0, 1] for fair weighting.
        trend_score = min(getattr(smc_ctx, "trend_strength", 0.5), 1.0)
        # Final score: 70% from signal engine (module + vote density) +
        #              30% from SMC trend conviction
        composite = round(sig.probability_score * 0.70 + trend_score * 0.30, 4)

        logger.debug(
            f"[{symbol}] {sig.direction} | "
            f"base_prob={sig.probability_score:.4f} "
            f"trend={trend_score:.4f} composite={composite:.4f}"
        )

        return (symbol, sig, current_price, smc_ctx, composite)

    # ─── Execution ────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult,
                       price: float, smc_ctx,
                       score: float = 0.0):
        stake = self.risk.calculate_stake()
        ac    = get_symbol_class(symbol)

        logger.info(
            f"▶ {sig.direction} {symbol} | ${stake:.2f} | "
            f"score={score:.4f} | struct={smc_ctx.structure} | "
            f"modules={sig.strength}/3 | "
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
            return  # contract still running

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

        outcome = "✅ WIN" if pnl > 0 else "❌ LOSS"
        logger.info(
            f"{outcome} | {symbol} | dir={direction} | "
            f"pnl=${pnl:+.4f} | stake=${stake:.2f} | "
            f"balance=${bal_after:.4f} | "
            f"open={self.risk._open_trade_count}/{self.risk.max_concurrent}"
        )

    # ─── Data initialisation ──────────────────────────────────────────────────

    async def _init_data(self, symbol: str):
        """
        Load historical OHLCV bars and subscribe to live ticks for one symbol.

        Uses a semaphore to cap concurrent API requests.
        Skips gracefully if the symbol is already being initialised.
        """
        if symbol in self._initializing or symbol in self._htf:
            return

        self._initializing.add(symbol)

        async with self._init_sem:
            try:
                logger.info(f"Loading data for {symbol} …")

                gran_ltf  = _ltf_granularity(symbol)
                bars_ltf  = _ltf_bars(symbol)

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

                if isinstance(htf_data, Exception):
                    logger.warning(
                        f"HTF data error for {symbol}: {htf_data}")
                    htf_data = []
                if isinstance(ltf_data, Exception):
                    logger.warning(
                        f"LTF data error for {symbol}: {ltf_data}")
                    ltf_data = []

                if not htf_data and not ltf_data:
                    logger.warning(
                        f"No historical data for {symbol} – skipping")
                    return

                if htf_data:
                    htf_b.seed(htf_data)
                if ltf_data:
                    ltf_b.seed(ltf_data)

                self._htf[symbol] = htf_b
                self._ltf[symbol] = ltf_b

                # Subscribe to live ticks
                await self.client.subscribe_ticks(
                    symbol,
                    lambda tick, s=symbol: self._on_tick(s, tick),
                )

                logger.info(
                    f"{symbol}: ready | "
                    f"htf={htf_b.count} bars (1h) | "
                    f"ltf={ltf_b.count} bars "
                    f"({'15m' if _is_forex(symbol) else '1m'})"
                )

            except Exception as exc:
                logger.error(
                    f"_init_data failed for {symbol}: {exc}\n"
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
