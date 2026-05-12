"""
bot_engine.py – Central async orchestrator (full version).

Integrates:
  • DerivClient           – WebSocket connection, ticks, trades
  • CandlestickBuilder    – tick → OHLCV per symbol (HTF + LTF)
  • SMCAnalyzer           – Phase A: HTF bias + SMC zones
  • SignalEngine          – Phase B: M1 / M2 / M3 modules
  • RiskManager           – Phase C: 9% daily limit + 1% compounding stake
  • NewsFilter            – calendar-based trade blackout
  • TradeJournal          – persistent trade log + analytics
  • SymbolManager         – adaptive priority queue + session awareness
  • keep_alive.update_status – feeds live data to the dashboard
"""

import asyncio
import logging
import time
import traceback
from dataclasses import asdict
from typing import Dict, List, Optional

import numpy as np

import config
import symbols as sym_module
from deriv_client import DerivClient
from candlestick_builder import CandlestickBuilder
from smc_analyzer import SMCAnalyzer
from signal_engine import SignalEngine
from risk_manager import RiskManager
from news_filter import NewsFilter
from trade_journal import TradeJournal
from symbol_manager import SymbolManager
import indicators as ind
from keep_alive import update_status
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

SCAN_INTERVAL         = 5      # seconds between symbol scans
SYMBOL_REFRESH_EVERY  = 3600   # re-fetch active symbols list every hour
DASHBOARD_PUSH_EVERY  = 15     # push dashboard state every N seconds


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

        # Per-symbol candlestick stores
        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}

        # Active contract tracking: contract_id → (symbol, direction, stake, entry)
        self._open_contracts: dict = {}

        # Scan queue (rebuilt each cycle)
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])
        self._queue_idx: int   = 0

        self._last_dashboard_push = 0.0
        self._last_symbol_refresh = 0.0

    # ─── Entry point ──────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Trading Bot  –  starting")
        logger.info("=" * 64)

        # Start WebSocket in background
        ws_task = asyncio.create_task(self.client.connect())

        # Wait up to 60 s for connection
        for _ in range(300):
            if self.client.is_connected:
                break
            await asyncio.sleep(1)

        if not self.client.is_connected:
            logger.error("Could not connect/authorise within 60 s")
            ws_task.cancel()
            return

        self.client.on_balance(self._on_balance)
        self.risk.set_balance(self.client.balance)

        # Discover tradeable symbols
        await self._refresh_symbols()

        # Run dashboard push loop in parallel
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

    # ─── Balance callback ─────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)

    # ─── Symbol discovery ─────────────────────────────────────────────────────

    async def _refresh_symbols(self):
        active_raw = await self.client.get_active_symbols()
        active_syms = [
            s["symbol"] for s in active_raw
            if s.get("exchange_is_open", 0) == 1 or
               any(s["symbol"].startswith(p)
                   for p in ("R_", "1HZ", "BOOM", "CRASH", "JD", "RDBEAR", "RDBULL", "stpRNG"))
        ]
        self.symbols.update_active(active_syms)
        self._queue = self.symbols.get_queue(max_symbols=50)
        self._last_symbol_refresh = time.time()
        logger.info(f"Symbol queue: {len(self._queue)} instruments")

    # ─── Dashboard push loop ──────────────────────────────────────────────────

  async def on_authorize(self, response):
    # Only start loading historical data after auth confirmed
    await self.load_all_symbols()

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
            running              = True,
            balance              = self.client.balance,
            day_start_balance    = self.risk.day_start_balance,
            paused_for_loss_limit= self.risk.is_paused,
            trades_today         = risk_s["total_trades"],
            wins_today           = risk_s["wins"],
            losses_today         = risk_s["losses"],
            session              = self.symbols.current_session,
            tradeable_count      = len(self._queue),
            gross_profit         = summary.get("gross_profit", 0),
            gross_loss           = summary.get("gross_loss", 0),
            profit_factor        = summary.get("profit_factor", 0),
            avg_rr               = summary.get("avg_rr", 0),
            best_trade           = summary.get("best_trade", 0),
            worst_trade          = summary.get("worst_trade", 0),
            streak               = summary.get("streak", 0),
            recent_trades        = self.journal.recent_trades(20),
            best_symbols         = self.symbols.best_symbols(10),
        )

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def _main_loop(self):
        while True:
            # Refresh symbol list hourly
            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()

            # Pause handling
            if self.risk.is_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(f"⛔ Trading paused – resumes in ~{mins:.0f} min")
                await asyncio.sleep(60)
                continue

            if not self._queue:
                logger.warning("Empty symbol queue – waiting 30 s")
                await asyncio.sleep(30)
                await self._refresh_symbols()
                continue

            symbol = self._next_symbol()
            update_status(current_symbol=symbol)
            logger.debug(f"Scanning {symbol}")

            try:
                await self._scan(symbol)
            except Exception as exc:
                logger.error(f"Scan error ({symbol}): {exc}")
                logger.debug(traceback.format_exc())

            await asyncio.sleep(SCAN_INTERVAL)

    def _next_symbol(self) -> str:
        if not self._queue:
            return sym_module.SYNTHETIC[0]
        sym = self._queue[self._queue_idx % len(self._queue)]
        self._queue_idx += 1
        # Rebuild queue every full rotation
        if self._queue_idx % len(self._queue) == 0:
            self._queue = self.symbols.get_queue(max_symbols=50)
        return sym

    # ─── Per-symbol scan (core SIFM pipeline) ────────────────────────────────

    async def _scan(self, symbol: str):
        # Lazy initialise data stores
        if symbol not in self._htf:
            await self._init_data(symbol)
            return   # wait for next cycle to have enough data

        htf = self._htf[symbol]
        ltf = self._ltf[symbol]

        if htf.count < 20 or ltf.count < 30:
            return

        # ── Phase A: HTF SMC ──────────────────────────────────────────────
        htf_bars = htf.completed_bars
        H = np.array([b.high  for b in htf_bars])
        L = np.array([b.low   for b in htf_bars])
        C = np.array([b.close for b in htf_bars])
        htf_atr_arr  = ind.atr(H, L, C, 14)
        valid_ha      = htf_atr_arr[~np.isnan(htf_atr_arr)]
        htf_atr       = float(valid_ha[-1]) if len(valid_ha) else 0.0

        smc_ctx = self.smc.analyse(htf_bars, htf_atr)
        if smc_ctx.bias == "NEUTRAL":
            return

        # ── Phase B.1: Price in SMC zone? ─────────────────────────────────
        ltf_bars_list = ltf.completed_bars
        if not ltf_bars_list:
            return
        current_price = float(ltf_bars_list[-1].close)

        in_zone = self.smc.price_in_smc_zone(current_price, smc_ctx.bias, smc_ctx)
        if not in_zone:
            return

        # ── Phase B.2–B.3: Signal modules ─────────────────────────────────
        sig = self.signal.evaluate(
            ltf_bars = ltf_bars_list,
            htf_bias = smc_ctx.bias,
            smc_ctx  = smc_ctx,
            in_zone  = in_zone,
        )

        update_status(last_signal=f"[{symbol}] {sig.direction} | {sig.reason}")

        if sig.direction == "NONE":
            return

        # ── Filters ────────────────────────────────────────────────────────
        if not self.risk.can_trade():
            return
        if self.news.is_blocked(symbol):
            logger.info(f"News block: {symbol}")
            return

        # Volatility filter: ATR(LTF) must not exceed 2×ATR(HTF)
        ltf_H = np.array([b.high  for b in ltf_bars_list])
        ltf_L = np.array([b.low   for b in ltf_bars_list])
        ltf_C = np.array([b.close for b in ltf_bars_list])
        ltf_atr_arr  = ind.atr(ltf_H, ltf_L, ltf_C, 14)
        valid_la      = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
        ltf_atr       = float(valid_la[-1]) if len(valid_la) else 0.0
        if htf_atr > 0 and ltf_atr > 2 * htf_atr:
            logger.info(f"Volatility filter: {symbol} skipped "
                        f"(ltf_atr={ltf_atr:.5f} > 2×htf_atr={htf_atr:.5f})")
            return

        # ── Phase C: Execute ───────────────────────────────────────────────
        await self._execute(symbol, sig, current_price, smc_ctx)

    # ─── Execution ────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig, price: float, smc_ctx):
        stake    = self.risk.calculate_stake()
        ac       = get_symbol_class(symbol)
        logger.info(f"▶ {sig.direction} {symbol} | ${stake:.2f} | "
                    f"struct={smc_ctx.structure} | modules={sig.strength}/3")

        buy_resp = await self.client.buy_contract(
            symbol   = symbol,
            direction= sig.direction,
            stake    = stake,
            duration = config.TRADE_DURATION,
            dur_unit = config.TRADE_DURATION_UNIT,
        )
        if not buy_resp:
            logger.warning(f"Order rejected for {symbol}")
            return

        cid   = str(buy_resp.get("contract_id", ""))
        bal_b = self.client.balance

        # Register with risk manager
        rec = self.risk.register_open(
            symbol=symbol, direction=sig.direction,
            stake=stake, entry_price=price)

        # Register with journal
        self.journal.open_trade(
            contract_id  = cid,
            symbol       = symbol,
            direction    = sig.direction,
            stake        = stake,
            entry_price  = price,
            balance_before = bal_b,
            asset_class  = ac,
            htf_bias     = smc_ctx.bias,
            smc_structure= smc_ctx.structure,
            m1=sig.m1_signal, m2=sig.m2_signal, m3=sig.m3_signal,
            modules      = sig.strength,
        )

        self._open_contracts[cid] = (symbol, sig.direction, stake, price, rec)

        # Subscribe to contract result
        await self.client.subscribe_contract(
            cid,
            lambda msg, _cid=cid: asyncio.create_task(
                self._on_contract_result(_cid, msg)))

    # ─── Contract result callback ─────────────────────────────────────────────

    async def _on_contract_result(self, cid: str, msg: dict):
        poc = msg.get("proposal_open_contract", {})
        if not poc.get("is_sold"):
            return   # still running

        sell_price = float(poc.get("sell_price", 0))
        pnl        = float(poc.get("profit",     0))
        payout     = float(poc.get("payout",     sell_price))
        bal_after  = self.client.balance

        info = self._open_contracts.pop(cid, None)
        if not info:
            return
        symbol, direction, stake, entry_price, rec = info

        # Update risk manager
        self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)

        # Update journal
        entry = self.journal.close_trade(
            contract_id = cid,
            exit_price  = sell_price,
            pnl         = pnl,
            payout      = payout,
            balance_after = bal_after,
        )

        # Update symbol manager (adaptive scoring)
        self.symbols.record_trade(symbol, won=pnl > 0, pnl=pnl)

        # Log result
        outcome = "✅ WIN" if pnl > 0 else "❌ LOSS"
        logger.info(f"{outcome} | {symbol} | pnl=${pnl:+.4f} | "
                    f"balance=${bal_after:.4f}")

    # ─── Data initialisation ──────────────────────────────────────────────────

    async def _init_data(self, symbol: str):
        logger.info(f"Loading data for {symbol} …")

        htf_b = CandlestickBuilder(granularity=config.HTF_GRANULARITY,
                                   max_bars=config.HTF_BARS + 20)
        ltf_b = CandlestickBuilder(granularity=config.LTF_GRANULARITY,
                                   max_bars=config.LTF_BARS + 20)

        htf_data = await self.client.get_candles(
            symbol, config.HTF_GRANULARITY, config.HTF_BARS)
        ltf_data = await self.client.get_candles(
            symbol, config.LTF_GRANULARITY, config.LTF_BARS)

        if not htf_data and not ltf_data:
            logger.warning(f"No historical data for {symbol} – skipping")
            return

        if htf_data:
            htf_b.seed(htf_data)
        if ltf_data:
            ltf_b.seed(ltf_data)

        self._htf[symbol] = htf_b
        self._ltf[symbol] = ltf_b

        # Live tick subscription
        await self.client.subscribe_ticks(
            symbol,
            lambda tick, s=symbol: self._on_tick(s, tick))

        logger.info(f"{symbol}: ready | htf={htf_b.count} | ltf={ltf_b.count}")

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
