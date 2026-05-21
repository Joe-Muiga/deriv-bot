"""
bot_engine.py – Central async orchestrator.
v16 — TRADE_SYMBOLS only. No SMC, no news filter, no HTF, no volatility indices.

Execution flow every cycle:
  1. Get eligible symbols from symbol_manager.get_queue()
  2. Fetch LTF bars for each symbol in parallel
  3. Run signal evaluation in parallel
  4. Rank signals by score (strength × 0.8 + session_win_rate × 0.2)
  5. Execute top N = current_concurrent_limit in parallel (if can_trade())
  6. Settlement handled by background asyncio.Task — scan continues immediately
  7. Orphaned contracts > CONTRACT_FORCE_CLOSE_SECS → record as loss

Cycle log:
  CYCLE X | Scanned: N | Signals: M | Executing: K | Open: J | Balance: $B | Streak: +S
"""

import asyncio
import datetime as _dt
import logging
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import config
from deriv_client import DerivClient
from candlestick_builder import CandlestickBuilder
from signal_engine import SignalEngine, SignalResult
from risk_manager import RiskManager, BotState
from symbol_manager import SymbolManager
from trade_journal import TradeJournal

logger = logging.getLogger(__name__)

DASHBOARD_PUSH_EVERY = 10


# ─── Result container ─────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol     : str
    sig        : SignalResult
    price      : float
    score      : float = 0.0


# ─── BotEngine ───────────────────────────────────────────────────────────────

class BotEngine:

    def __init__(self):
        self._cycle_count    : int  = 0
        self._running        : bool = False
        self._open_contracts : Dict[str, dict]  = {}    # contract_id → meta
        self._contract_open_times : Dict[str, float] = {}
        self._active_symbols : Set[str] = set()
        self._settle_tasks   : Set[asyncio.Task] = set()

        self._ltf : Dict[str, CandlestickBuilder] = {}

        self._confirmed_daily_loss : float = 0.0
        self._day_start_balance    : float = 0.0
        self._confirmed_paused     : bool  = False
        self._current_utc_day      : int   = -1

        self.client  = DerivClient()
        self.risk    = RiskManager(
            risk_per_trade = config.RISK_PER_TRADE_PCT,
            min_stake      = config.MIN_STAKE,
            max_stake      = config.MAX_STAKE,
            max_concurrent = config.MAX_CONCURRENT_TRADES,
            deriv_client   = self.client,
        )
        self.signal  = SignalEngine(symbols=list(config.TRADE_SYMBOLS), config=config)
        self.symbols = SymbolManager()
        self.journal = TradeJournal()

    # ─── LTF granularity ─────────────────────────────────────────────────────

    @staticmethod
    def _ltf_gran(symbol: str) -> int:
        return config.LTF_GRANULARITY   # 60s for all trade symbols

    # ─── Composite score ─────────────────────────────────────────────────────

    def _composite_score(self, scan: ScanResult) -> float:
        sig_score = getattr(scan.sig, "score", scan.sig.strength / 3.0)
        win_rate  = self.symbols.win_rate(scan.symbol)
        return round(sig_score * 0.8 + win_rate * 0.2, 4)

    # ─── Initialise symbol data ───────────────────────────────────────────────

    async def _init_symbol(self, symbol: str) -> bool:
        try:
            gran = self._ltf_gran(symbol)
            bars = await self.client.get_candles(symbol, gran, config.LTF_BARS)
            if not bars:
                return False
            cb = CandlestickBuilder(granularity=gran)
            for b in bars:
                cb.update(b)
            self._ltf[symbol] = cb
            logger.info(f"INIT {symbol} | {len(bars)} LTF bars loaded")
            return True
        except Exception as exc:
            logger.error(f"INIT FAILED {symbol}: {exc}")
            return False

    async def _init_all_symbols(self) -> None:
        tasks = [self._init_symbol(sym) for sym in config.TRADE_SYMBOLS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        logger.info(f"Initialised {ok}/{len(config.TRADE_SYMBOLS)} symbols")

    # ─── Fetch latest candles ─────────────────────────────────────────────────

    async def _refresh_ltf(self, symbol: str) -> None:
        try:
            gran = self._ltf_gran(symbol)
            bars = await self.client.get_candles(symbol, gran, 5)
            if bars and symbol in self._ltf:
                for b in bars:
                    self._ltf[symbol].update(b)
        except Exception:
            pass

    # ─── Single symbol scan ───────────────────────────────────────────────────

    async def _scan_symbol(self, symbol: str) -> Optional[ScanResult]:
        try:
            await self._refresh_ltf(symbol)

            cb = self._ltf.get(symbol)
            if cb is None:
                return None
            ltf_bars = cb.candles
            if len(ltf_bars) < 20:
                return None

            price = float(ltf_bars[-1].close if hasattr(ltf_bars[-1], "close") else ltf_bars[-1]["close"])

            sig = self.signal.evaluate(ltf_bars, symbol=symbol)

            if not sig.emitted or sig.direction == "NONE":
                return None

            scan = ScanResult(symbol=symbol, sig=sig, price=price, score=0.0)
            scan.score = self._composite_score(scan)
            return scan

        except Exception as exc:
            logger.error(f"_scan_symbol {symbol}: {exc}")
            return None

    # ─── Execute a single trade ───────────────────────────────────────────────

    async def _execute(self, scan: ScanResult) -> None:
        symbol    = scan.symbol
        direction = scan.sig.direction

        if not self.risk.can_trade():
            return
        if symbol in self._active_symbols:
            return

        stake = await self.risk.calculate_stake()
        gran  = self._ltf_gran(symbol)

        contract_id = await self.client.buy_contract(
            symbol    = symbol,
            direction = direction,
            stake     = stake,
            duration  = config.TRADE_DURATION,
            duration_unit = config.TRADE_DURATION_UNIT,
        )

        if contract_id is None:
            return

        self._active_symbols.add(symbol)
        self._contract_open_times[contract_id] = time.time()
        self.symbols.record_trade_placed(symbol)

        rec = self.risk.register_open(
            symbol      = symbol,
            direction   = direction,
            stake       = stake,
            entry_price = scan.price,
        )

        self._open_contracts[contract_id] = {
            "symbol":    symbol,
            "direction": direction,
            "stake":     stake,
            "record":    rec,
            "opened_at": time.time(),
        }

        # Register result callback
        def _on_result(result: dict):
            asyncio.create_task(self._settle(contract_id, result))

        self.client.register_contract_callback(contract_id, _on_result)

        task = asyncio.create_task(
            self._orphan_watchdog(contract_id, config.CONTRACT_FORCE_CLOSE_AFTER_SECONDS))
        self._settle_tasks.add(task)
        task.add_done_callback(self._settle_tasks.discard)

    # ─── Settle a closed contract ─────────────────────────────────────────────

    async def _settle(self, contract_id: str, result: dict) -> None:
        meta = self._open_contracts.pop(contract_id, None)
        if meta is None:
            return

        symbol     = meta["symbol"]
        rec        = meta["record"]
        self._active_symbols.discard(symbol)
        self._contract_open_times.pop(contract_id, None)

        try:
            profit = float(result.get("profit", 0.0))
            won    = profit > 0
            exit_p = float(result.get("exit_tick", 0.0))

            self.risk.register_close(rec, exit_p, profit)
            self.symbols.record_result(symbol, won)

            try:
                self.journal.record(
                    symbol    = symbol,
                    direction = meta["direction"],
                    stake     = meta["stake"],
                    pnl       = profit,
                    won       = won,
                )
            except Exception:
                pass

        except Exception as exc:
            logger.error(f"_settle error {contract_id}: {exc}")

    # ─── Orphan watchdog ──────────────────────────────────────────────────────

    async def _orphan_watchdog(self, contract_id: str, timeout_secs: int) -> None:
        await asyncio.sleep(timeout_secs)
        if contract_id not in self._open_contracts:
            return
        logger.warning(f"ORPHAN {contract_id}: force-closing after {timeout_secs}s")
        meta = self._open_contracts.pop(contract_id, None)
        if meta is None:
            return
        symbol = meta["symbol"]
        rec    = meta["record"]
        self._active_symbols.discard(symbol)
        self._contract_open_times.pop(contract_id, None)
        self.risk.register_close(rec, 0.0, -rec.stake)
        self.symbols.record_result(symbol, False)

    # ─── Daily loss check ─────────────────────────────────────────────────────

    def _check_daily_loss(self) -> bool:
        pnl_pct = self.risk.daily_pnl_pct
        limit   = getattr(config, "DAILY_LOSS_LIMIT_PCT", 0.15)
        if pnl_pct <= -limit:
            if not self._confirmed_paused:
                logger.warning(
                    f"DAILY LOSS LIMIT HIT: {pnl_pct*100:.2f}% | "
                    f"Pausing until midnight UTC")
                self._confirmed_paused = True
                self.risk.set_bot_state(BotState.DRAINING)
            return True
        return False

    def _handle_day_rollover(self) -> None:
        day = _dt.datetime.utcnow().day
        if day != self._current_utc_day:
            self._current_utc_day  = day
            self._confirmed_paused = False
            if self.risk.bot_state == BotState.DRAINING:
                self.risk.set_bot_state(BotState.RUNNING)
            logger.info("Day rolled over — resuming trading")

    # ─── Main scan loop ───────────────────────────────────────────────────────

    async def _scan_cycle(self) -> None:
        self._cycle_count += 1
        self.risk.tick_cycle()
        self.symbols.reset_cycle_used()
        self._handle_day_rollover()

        if self._check_daily_loss():
            return

        eligible = self.symbols.get_queue()
        if not eligible:
            return

        # Parallel scan
        scan_tasks = [self._scan_symbol(sym) for sym in eligible]
        raw = await asyncio.gather(*scan_tasks, return_exceptions=True)
        signals: List[ScanResult] = [
            r for r in raw
            if isinstance(r, ScanResult) and r is not None
        ]

        # Rank
        signals.sort(key=lambda s: s.score, reverse=True)

        slots_available = (self.risk.current_concurrent_limit
                           - len(self._open_contracts))
        to_execute = signals[:max(0, slots_available)]

        # Execute in parallel
        if to_execute and self.risk.can_trade():
            await asyncio.gather(
                *[self._execute(scan) for scan in to_execute],
                return_exceptions=True,
            )

        bal    = self.risk.current_balance
        streak = self.risk.current_streak
        sign   = "+" if streak >= 0 else ""
        logger.info(
            f"CYCLE {self._cycle_count} | "
            f"Scanned: {len(eligible)} | "
            f"Signals: {len(signals)} | "
            f"Executing: {len(to_execute)} | "
            f"Open: {len(self._open_contracts)} | "
            f"Balance: ${bal:.2f} | "
            f"Streak: {sign}{streak}")

    # ─── Redeploy ─────────────────────────────────────────────────────────────

    async def _maybe_redeploy(self) -> None:
        n = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)
        url = getattr(config, "RENDER_DEPLOY_HOOK_URL", "")
        if url and self._cycle_count % n == 0:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    await session.post(url, timeout=aiohttp.ClientTimeout(total=5))
                logger.info("Redeploy hook triggered")
            except Exception:
                pass

    # ─── Entry point ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("BotEngine starting — connecting to Deriv...")
        await self.client.connect()

        balance = await self.client.get_balance()
        if balance:
            self.risk.set_balance(balance)
            self._day_start_balance = balance
            logger.info(f"Initial balance: ${balance:.4f}")

        await self._init_all_symbols()
        self.symbols.start_midnight_reset_task()
        self._running = True

        logger.info(
            f"Bot running | Trade symbols: {config.TRADE_SYMBOLS} | "
            f"Concurrent limit: {self.risk.current_concurrent_limit}")

        while self._running:
            try:
                await self._scan_cycle()
                await self._maybe_redeploy()
            except Exception as exc:
                logger.error(f"Scan cycle error: {exc}\n{traceback.format_exc()}")
            await asyncio.sleep(config.SCAN_CYCLE_SLEEP)

    def stop(self) -> None:
        self._running = False
        self.risk.set_bot_state(BotState.STOPPED)
        logger.info("BotEngine stopped")
