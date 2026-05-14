"""
bot_engine.py – Central async orchestrator (parallel-scan edition).

Architecture changes vs original:
  • ALL symbols are scanned simultaneously every cycle via asyncio.gather.
  • _scan() returns an Optional[ScanResult] instead of executing directly.
  • _main_loop picks the single highest-strength signal across all symbols
    and executes it immediately, then begins the next cycle.
  • Symbols are initialised in parallel batches at startup so scanning can
    begin as soon as possible.
  • Forex pairs use 15-min LTF; all other assets use 1-min LTF.
  • No per-symbol idle time or sequential queue rotation.
"""

import asyncio
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
from keep_alive import update_status
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

# How long to sleep between full-parallel scan cycles (keeps CPU sane)
SCAN_CYCLE_SLEEP     = 2
DASHBOARD_PUSH_EVERY = 15
SYMBOL_REFRESH_EVERY = 3600
INIT_BATCH_SIZE      = 10   # symbols initialised in parallel per batch


# ─── Result container returned by _scan ──────────────────────────────────────

@dataclass
class ScanResult:
    symbol:  str
    sig:     SignalResult
    price:   float
    smc_ctx: SMCContext
    ltf_atr: float
    htf_atr: float


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

        # Per-symbol candlestick stores  {symbol: CandlestickBuilder}
        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}

        # Tracks symbols currently being initialised (prevents duplicate fetches)
        self._initializing: set = set()

        # Active contract tracking: contract_id → (symbol, direction, stake, entry, rec)
        self._open_contracts: dict = {}

        # Full symbol list (rebuilt on refresh)
        self._queue: List[str] = list(sym_module.SYNTHETIC[:5])

        self._last_symbol_refresh = 0.0

    # ─── Timeframe routing ────────────────────────────────────────────────────

    @staticmethod
    def _ltf_gran(symbol: str) -> int:
        """15-min LTF for forex pairs, 1-min for everything else."""
        return (config.FOREX_LTF_GRANULARITY
                if symbol in sym_module.FOREX
                else config.OTHER_LTF_GRANULARITY)

    # ─── Entry point ──────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 64)
        logger.info("  SIFM Deriv Trading Bot  –  parallel-scan mode")
        logger.info("=" * 64)

        ws_task = asyncio.create_task(self.client.connect())

        # Wait up to 60 s for authorisation
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

        await self._refresh_symbols()
        await self._init_all_symbols()          # parallel batch init

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
        synth_pfx  = ("R_", "1HZ", "BOOM", "CRASH", "JD",
                       "RDBEAR", "RDBULL", "stpRNG")
        active_syms = [
            s["symbol"] for s in active_raw
            if s.get("exchange_is_open", 0) == 1
            or any(s["symbol"].startswith(p) for p in synth_pfx)
        ]
        self.symbols.update_active(active_syms)
        # Request the full un-filtered symbol list (no cooldown filtering)
        self._queue = self.symbols.get_queue(max_symbols=200)
        self._last_symbol_refresh = time.time()
        logger.info(f"Symbol queue: {len(self._queue)} instruments")

    # ─── Bulk initialisation ──────────────────────────────────────────────────

    async def _init_all_symbols(self):
        """Initialise all queued symbols in parallel batches."""
        uninit = [s for s in self._queue if s not in self._htf]
        logger.info(
            f"Initialising {len(uninit)} symbols in batches of {INIT_BATCH_SIZE} …")
        for i in range(0, len(uninit), INIT_BATCH_SIZE):
            batch = uninit[i : i + INIT_BATCH_SIZE]
            await asyncio.gather(
                *[self._init_data(s) for s in batch],
                return_exceptions=True)
        logger.info(f"{len(self._htf)} symbols ready for scanning")

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
        while True:

            # Hourly symbol refresh
            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()
                await self._init_all_symbols()

            # Only pause is triggered by 90% drawdown
            if self.risk.is_paused:
                mins = self.risk.minutes_until_midnight()
                logger.info(
                    f"⛔ 90% drawdown – trading paused, resumes in ~{mins:.0f} min")
                await asyncio.sleep(60)
                continue

            # Collect all initialised symbols
            ready = [s for s in self._queue if s in self._htf]
            if not ready:
                await asyncio.sleep(5)
                continue

            update_status(current_symbol=f"[parallel] {len(ready)} symbols")

            # ── Scan ALL symbols simultaneously ──────────────────────────────
            raw = await asyncio.gather(
                *[self._scan(s) for s in ready],
                return_exceptions=True)

            # Filter to valid, confirmed signals
            valid: List[ScanResult] = [
                r for r in raw
                if isinstance(r, ScanResult) and r.sig.direction != "NONE"
            ]

            if valid:
                # Pick the single highest-probability signal
                valid.sort(key=lambda r: r.sig.strength, reverse=True)
                best = valid[0]

                update_status(
                    current_symbol = best.symbol,
                    last_signal    = (f"[{best.symbol}] {best.sig.direction}"
                                      f" | {best.sig.reason}"),
                )
                logger.info(
                    f"Best signal: {best.symbol} {best.sig.direction} "
                    f"strength={best.sig.strength}/3 "
                    f"(from {len(valid)} valid signals across {len(ready)} symbols)")

                if self.risk.can_trade():
                    try:
                        await self._execute(
                            best.symbol, best.sig, best.price, best.smc_ctx)
                    except Exception as exc:
                        logger.error(
                            f"Execute error ({best.symbol}): {exc}\n"
                            f"{traceback.format_exc()}")

            await asyncio.sleep(SCAN_CYCLE_SLEEP)

    # ─── Per-symbol scan ──────────────────────────────────────────────────────

    async def _scan(self, symbol: str) -> Optional[ScanResult]:
        """
        Evaluate one symbol through the full SIFM pipeline.
        Returns a ScanResult if a tradeable signal exists, None otherwise.
        Never raises – all exceptions are caught internally.
        """
        try:
            htf = self._htf.get(symbol)
            ltf = self._ltf.get(symbol)
            if htf is None or ltf is None:
                return None

            if htf.count < 20 or ltf.count < 30:
                return None

            # ── Phase A: HTF SMC ──────────────────────────────────────────
            htf_bars = htf.completed_bars
            H = np.array([b.high  for b in htf_bars])
            L = np.array([b.low   for b in htf_bars])
            C = np.array([b.close for b in htf_bars])
            htf_atr_arr = ind.atr(H, L, C, 14)
            valid_ha    = htf_atr_arr[~np.isnan(htf_atr_arr)]
            htf_atr     = float(valid_ha[-1]) if len(valid_ha) else 0.0

            smc_ctx = self.smc.analyse(htf_bars, htf_atr)
            if smc_ctx.bias == "NEUTRAL":
                return None

            # ── Phase B.1: Price in SMC zone? ─────────────────────────────
            ltf_bars_list = ltf.completed_bars
            if not ltf_bars_list:
                return None
            current_price = float(ltf_bars_list[-1].close)

            in_zone = self.smc.price_in_smc_zone(
                current_price, smc_ctx.bias, smc_ctx)
            if not in_zone:
                return None

            # ── Phase B.2–B.3: Signal modules ─────────────────────────────
            sig = self.signal.evaluate(
                ltf_bars = ltf_bars_list,
                htf_bias = smc_ctx.bias,
                smc_ctx  = smc_ctx,
                in_zone  = in_zone,
            )
            if sig.direction == "NONE":
                return None

            # ── News filter ────────────────────────────────────────────────
            if self.news.is_blocked(symbol):
                logger.debug(f"News block: {symbol}")
                return None

            # ── Volatility filter ──────────────────────────────────────────
            ltf_H = np.array([b.high  for b in ltf_bars_list])
            ltf_L = np.array([b.low   for b in ltf_bars_list])
            ltf_C = np.array([b.close for b in ltf_bars_list])
            ltf_atr_arr = ind.atr(ltf_H, ltf_L, ltf_C, 14)
            valid_la    = ltf_atr_arr[~np.isnan(ltf_atr_arr)]
            ltf_atr     = float(valid_la[-1]) if len(valid_la) else 0.0

            if htf_atr > 0 and ltf_atr > 2 * htf_atr:
                logger.debug(
                    f"Volatility filter: {symbol} skipped "
                    f"(ltf_atr={ltf_atr:.5f} > 2×htf_atr={htf_atr:.5f})")
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
            logger.debug(f"_scan({symbol}) suppressed error: {exc}")
            return None

    # ─── Execution ────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult,
                       price: float, smc_ctx: SMCContext):
        stake = self.risk.calculate_stake()
        ac    = get_symbol_class(symbol)
        logger.info(
            f"▶ {sig.direction} {symbol} | ${stake:.2f} | "
            f"struct={smc_ctx.structure} | modules={sig.strength}/3")

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
                self._on_contract_result(_cid, msg)))

    # ─── Contract result callback ─────────────────────────────────────────────

    async def _on_contract_result(self, cid: str, msg: dict):
        poc = msg.get("proposal_open_contract", {})
        if not poc.get("is_sold"):
            return   # contract still running

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

        # Update scoring stats only – no cooldowns applied
        self.symbols.record_trade(symbol, won=pnl > 0, pnl=pnl)

        outcome = "✅ WIN" if pnl > 0 else "❌ LOSS"
        logger.info(
            f"{outcome} | {symbol} | pnl=${pnl:+.4f} | balance=${bal_after:.4f}")

    # ─── Data initialisation ──────────────────────────────────────────────────

    async def _init_data(self, symbol: str):
        """Fetch historical candles and subscribe to live ticks for one symbol."""
        if symbol in self._initializing or symbol in self._htf:
            return
        self._initializing.add(symbol)
        try:
            ltf_gran = self._ltf_gran(symbol)
            logger.debug(f"Init {symbol} | htf={config.HTF_GRANULARITY}s "
                         f"ltf={ltf_gran}s")

            htf_b = CandlestickBuilder(
                granularity = config.HTF_GRANULARITY,
                max_bars    = config.HTF_BARS + 20)
            ltf_b = CandlestickBuilder(
                granularity = ltf_gran,
                max_bars    = config.LTF_BARS + 20)

            # Fetch HTF and LTF data concurrently
            htf_data, ltf_data = await asyncio.gather(
                self.client.get_candles(
                    symbol, config.HTF_GRANULARITY, config.HTF_BARS),
                self.client.get_candles(
                    symbol, ltf_gran, config.LTF_BARS),
                return_exceptions=True,
            )

            if isinstance(htf_data, Exception):
                htf_data = []
            if isinstance(ltf_data, Exception):
                ltf_data = []

            if not htf_data and not ltf_data:
                logger.warning(f"No historical data for {symbol} – skipping")
                return

            if htf_data:
                htf_b.seed(htf_data)
            if ltf_data:
                ltf_b.seed(ltf_data)

            self._htf[symbol] = htf_b
            self._ltf[symbol] = ltf_b

            # Subscribe to live ticks (feeds both builders)
            await self.client.subscribe_ticks(
                symbol,
                lambda tick, s=symbol: self._on_tick(s, tick))

            logger.info(
                f"{symbol}: ready | htf={htf_b.count}bars "
                f"ltf={ltf_b.count}bars (ltf_gran={ltf_gran}s)")

        except Exception as exc:
            logger.error(f"_init_data({symbol}) failed: {exc}")
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
