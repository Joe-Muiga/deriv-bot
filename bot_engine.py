"""
bot_engine.py – Central async orchestrator (parallel, always-3/3 edition).

Signal quality gates (ALL must pass before a trade fires):
  1. SMC zone check      – price inside a valid HTF order block / FVG
  2. 3/3 signal modules  – M1 (EMA+RSI), M2 (candlestick), M3 (vote) all agree
  3. 5/7 indicator votes – Module 3 quantitative gate
  4. Volatility filter   – LTF ATR ≤ 2× HTF ATR
  5. News filter         – no high-impact event within NEWS_BLOCK_MINUTES
  6. Freshness guard     – MIN_SECONDS_BETWEEN_SAME_SYMBOL since last trade

No streak-based blocking.  A qualifying signal always executes.
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
            min_modules = config.MIN_MODULES_FOR_SIGNAL,   # always 3
            min_votes   = config.MIN_INDICATOR_VOTES,      # always 5
        )
        self.news    = NewsFilter(block_minutes=config.NEWS_BLOCK_MINUTES)
        self.journal = TradeJournal()
        self.symbols = SymbolManager()

        # Per-symbol OHLCV stores
        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}

        self._initializing: Set[str] = set()
        self._init_sem = asyncio.Semaphore(config.INIT_BATCH_SIZE)

        # Open contracts: contract_id → (symbol, direction, stake, entry, rec)
        self._open_contracts: dict = {}

        # Symbol queue
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])

        # Freshness guard: symbol → epoch time of last executed trade
        self._last_traded_time: Dict[str, float] = {}

        # Stats for dashboard only (never used to gate trades)
        self._daily_trades:        int = 0
        self._consecutive_losses:  int = 0
        self._consecutive_wins:    int = 0
        self._current_day:         str = ""

        self._last_symbol_refresh = 0.0

    # ─── Entry point ──────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Bot  –  always-3/3, parallel edition")
        logger.info("=" * 64)

        self._reset_day_if_needed()
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
            logger.critical(f"Main loop crashed: {exc}\n{traceback.format_exc()}")
        finally:
            dash_task.cancel()
            balance_task.cancel()
            ws_task.cancel()

    # ─── Balance callback ─────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)
        logger.info(f"Balance sync: ${balance:.4f}")

    # ─── Day tracking (dashboard only) ────────────────────────────────────────

    def _reset_day_if_needed(self):
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._current_day:
            self._current_day       = today
            self._daily_trades      = 0
            self._consecutive_losses = 0
            self._consecutive_wins   = 0
            logger.info(f"New UTC day: {today}")

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
            f"Queue refreshed: {len(self._queue)} symbols | "
            f"session: {self.symbols.current_session}"
        )

    # ─── Parallel initialisation ──────────────────────────────────────────────

    async def _ensure_all_initialized(self):
        pending = [s for s in self._queue
                   if s not in self._htf and s not in self._initializing]
        if not pending:
            return
        await asyncio.gather(
            *[self._init_data(s) for s in pending],
            return_exceptions=True,
        )

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
        update_status(
            running               = True,
            balance               = self.client.balance,
            day_start_balance     = self.risk.day_start_balance,
            paused_for_loss_limit = self.risk.is_paused,
            daily_trades          = self._daily_trades,
            consecutive_losses    = self._consecutive_losses,
            consecutive_wins      = self._consecutive_wins,
            trades_today          = risk_s["total_trades"],
            wins_today            = risk_s["wins"],
            losses_today          = risk_s["losses"],
            win_rate              = risk_s["win_rate"],
            session               = self.symbols.current_session,
            tradeable_count       = len(self._queue),
            open_trades           = risk_s["open_trades"],
            gross_profit          = summary.get("gross_profit", 0),
            gross_loss            = summary.get("gross_loss", 0),
            profit_factor         = summary.get("profit_factor", 0),
            best_trade            = summary.get("best_trade", 0),
            worst_trade           = summary.get("worst_trade", 0),
            recent_trades         = self.journal.recent_trades(20),
            best_symbols          = self.symbols.best_symbols(10),
        )

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def _main_loop(self):
        while True:
            cycle_start = time.time()

            self._reset_day_if_needed()

            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()

            # 90% daily loss limit pause
            if self.risk.is_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(
                    f"⛔ Paused (90% daily loss) | "
                    f"resumes in ~{mins:.0f} min"
                )
                await asyncio.sleep(60)
                continue

            if not self._queue:
                await asyncio.sleep(30)
                await self._refresh_symbols()
                continue

            await self._ensure_all_initialized()

            # Scan all symbols in parallel
            candidates = await self._scan_all_parallel()

            # Rank by composite probability score
            candidates.sort(key=lambda x: x[4], reverse=True)

            if candidates:
                top = candidates[0]
                logger.info(
                    f"🏆 Best: [{top[0]}] {top[1].direction} | "
                    f"score={top[4]:.4f} | {top[1].reason}"
                )

            # Execute ALL qualifying signals up to concurrent limit
            # No streak check — if it passed all 6 gates, it trades
            executed = 0
            for sym, sig, price, smc_ctx, score in candidates:
                if not self.risk.can_trade():
                    break
                update_status(
                    current_symbol = sym,
                    last_signal    = f"[{sym}] {sig.direction} score={score:.4f}",
                )
                await self._execute(sym, sig, price, smc_ctx, score)
                executed += 1

            if candidates and executed == 0:
                logger.debug(
                    f"Signals ready but all slots full "
                    f"({self.risk._open_trade_count}/"
                    f"{self.risk.max_concurrent} open)"
                )

            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0.1, SCAN_INTERVAL - elapsed))

    # ─── Parallel signal collection ───────────────────────────────────────────

    async def _scan_all_parallel(self) -> List[Tuple]:
        ready = [s for s in self._queue if s in self._htf]
        if not ready:
            return []

        results = await asyncio.gather(
            *[self._scan_for_signal(s) for s in ready],
            return_exceptions=True,
        )

        valid = []
        for sym, res in zip(ready, results):
            if isinstance(res, Exception):
                logger.debug(f"Scan error [{sym}]: {res}")
            elif res is not None:
                valid.append(res)
        return valid

    # ─── Single-symbol evaluation (pure – no side effects) ───────────────────

    async def _scan_for_signal(self, symbol: str) -> Optional[Tuple]:
        """
        Returns (symbol, sig, price, smc_ctx, composite_score) or None.
        A result here means ALL 6 gates have been cleared.
        """
        htf = self._htf.get(symbol)
        ltf = self._ltf.get(symbol)
        if htf is None or ltf is None:
            return None
        if htf.count < 20 or ltf.count < 30:
            return None

        # Gate 1 – Freshness guard (time-based, not streak-based)
        last_t = self._last_traded_time.get(symbol, 0.0)
        if time.time() - last_t < config.MIN_SECONDS_BETWEEN_SAME_SYMBOL:
            return None

        # Gate 2 – HTF SMC structure
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

        # Gate 3 – Price in SMC zone
        ltf_bars_list = ltf.completed_bars
        if not ltf_bars_list:
            return None
        current_price = float(ltf_bars_list[-1].close)
        in_zone = self.smc.price_in_smc_zone(
            current_price, smc_ctx.bias, smc_ctx)
        if not in_zone:
            return None

        # Gate 4 – 3/3 modules + 5/7 votes
        sig = self.signal.evaluate(
            ltf_bars    = ltf_bars_list,
            htf_bias    = smc_ctx.bias,
            smc_ctx     = smc_ctx,
            in_zone     = in_zone,
            min_modules = config.MIN_MODULES_FOR_SIGNAL,
            min_votes   = config.MIN_INDICATOR_VOTES,
        )
        if sig.direction == "NONE":
            return None

        # Gate 5 – News filter
        if self.news.is_blocked(symbol):
            logger.debug(f"[{symbol}] Skipped: news block")
            return None

        # Gate 6 – Volatility filter
        ltf_H = np.array([b.high  for b in ltf_bars_list])
        ltf_L = np.array([b.low   for b in ltf_bars_list])
        ltf_C = np.array([b.close for b in ltf_bars_list])
        ltf_atr_arr = ind.atr(ltf_H, ltf_L, ltf_C, 14)
        valid_la    = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
        ltf_atr     = float(valid_la[-1]) if len(valid_la) else 0.0
        if htf_atr > 0 and ltf_atr > 2 * htf_atr:
            logger.debug(f"[{symbol}] Skipped: high volatility")
            return None

        # Composite score: 70% signal quality + 30% SMC trend strength
        trend_score = min(getattr(smc_ctx, "trend_strength", 0.5), 1.0)
        composite   = round(sig.probability_score * 0.70 + trend_score * 0.30, 4)

        logger.debug(
            f"[{symbol}] ✓ All gates cleared | "
            f"{sig.direction} | score={composite:.4f}"
        )
        return (symbol, sig, current_price, smc_ctx, composite)

    # ─── Execution ────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult,
                       price: float, smc_ctx, score: float = 0.0):
        stake = self.risk.calculate_stake()
        ac    = get_symbol_class(symbol)

        logger.info(
            f"▶ {sig.direction} {symbol} | stake=${stake:.2f} | "
            f"score={score:.4f} | modules=3/3 | "
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

        # Freshness guard timestamp — set on open, regardless of outcome
        self._last_traded_time[symbol] = time.time()

        await self.client.subscribe_contract(
            cid,
            lambda msg, _cid=cid: asyncio.create_task(
                self._on_contract_result(_cid, msg)
            ),
        )

    # ─── Contract result ──────────────────────────────────────────────────────

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

        # Dashboard counters only — never used to block trades
        self._daily_trades += 1
        if won:
            self._consecutive_wins  += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins   = 0

        outcome = "✅ WIN" if won else "❌ LOSS"
        logger.info(
            f"{outcome} | {symbol} {direction} | "
            f"pnl=${pnl:+.4f} | balance=${bal_after:.4f} | "
            f"streak: {self._consecutive_wins}W / {self._consecutive_losses}L"
        )

    # ─── Data initialisation ──────────────────────────────────────────────────

    async def _init_data(self, symbol: str):
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
                    f"{symbol}: ready | "
                    f"htf={htf_b.count}×1h | ltf={ltf_b.count}×{ltf_label}"
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
        epoch = int(tick.get("epoch", time.time()))
        price = float(tick.get("quote", 0))
        if price == 0:
            return
        if symbol in self._ltf:
            self._ltf[symbol].add_tick(epoch, price)
        if symbol in self._htf:
            self._htf[symbol].add_tick(epoch, price)
