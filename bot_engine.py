"""
bot_engine.py – Central async orchestrator.

v17 — Continuous non-blocking parallel-scan rewrite.

KEY CHANGES vs v16
───────────────────
INIT:
  • Only config.TRADE_SYMBOLS are initialised — no volatility-index /
    ALL_TRADE_SYMBOLS phased startup. Symbols that fail to initialise are
    skipped; the bot never crashes on a single symbol failure.

MAIN LOOP:
  • Scanning and settling are fully decoupled. The main loop scans every
    ready symbol in parallel every cycle via asyncio.gather and does NOT
    wait for open contracts to settle before scanning again.
  • The only execution gate is concurrent-slot availability
    (self.risk.current_concurrent_limit).
  • Signals are ranked by score*0.85 + symbol_win_rate*0.15, deduplicated
    (highest score per symbol kept), and the top N ranked signals are
    executed in parallel via asyncio.gather.

SETTLING:
  • A separate `_settle_loop` task runs independently of the scan loop.
    After every settle wait it resolves orphaned contracts and increments
    `_cycle_count`; once REDEPLOY_EVERY_N_CYCLES is reached it drains all
    open contracts and triggers a Render redeploy.

UNCHANGED:
  • Websocket connection (DerivClient), dashboard serving (_push_dashboard /
    _dashboard_loop), keep_alive integration (record_signal, record_trade,
    record_failure, update_open_contracts, set_active_trades, update_status),
    and the Render redeploy mechanism (restart_scheduler.trigger_redeploy /
    is_redeploy_pending).
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
INIT_BATCH_SIZE       = getattr(config, "INIT_BATCH_SIZE", 10)

CONTRACT_MAX_AGE_SECS     = getattr(config, "CONTRACT_MAX_AGE_SECS",      120)
CONTRACT_FORCE_CLOSE_SECS = getattr(config, "CONTRACT_FORCE_CLOSE_SECS",  300)


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
        self._cycle_count:            int                = 0
        self._running:                bool               = False
        self._session_start_balance:  float              = 0.0
        self._open_contracts:         dict               = {}
        self._contract_open_times:    dict               = {}

        self._htf:                    Dict[str, CandlestickBuilder] = {}
        self._mtf:                    Dict[str, CandlestickBuilder] = {}
        self._ltf:                    Dict[str, CandlestickBuilder] = {}

        self._initialised_symbols:    Set[str]           = set()
        self._initializing:           Set[str]           = set()

        self._confirmed_daily_loss:   float              = 0.0
        self._day_start_balance:      float              = 0.0
        self._confirmed_paused:       bool               = False
        self._current_utc_day:        int                = -1

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
                    logger.warning("DAILY LIMIT HIT — paused until midnight UTC")
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
        logger.info("  SIFM Deriv Trading Bot  –  continuous parallel-scan")
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

        await self._init_all_symbols()

        try:
            self._push_dashboard()
        except Exception:
            pass

        dash_task   = asyncio.create_task(self._dashboard_loop())
        settle_task = asyncio.create_task(self._settle_loop())

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.critical(f"Main loop crashed: {exc}\n{traceback.format_exc()}")
        finally:
            dash_task.cancel()
            settle_task.cancel()
            ws_task.cancel()

    # ── Startup — TRADE_SYMBOLS only ───────────────────────────────────────────

    async def _init_all_symbols(self):
        trade_symbols = list(getattr(config, "TRADE_SYMBOLS", []))
        logger.info(f"Initialising {len(trade_symbols)} trade symbols")

        results = await asyncio.gather(
            *[self._init_data(s) for s in trade_symbols],
            return_exceptions=True,
        )
        for s, r in zip(trade_symbols, results):
            if isinstance(r, Exception):
                logger.warning(f"{s}: failed to initialise — skipping ({r})")

        self._initialised_symbols = set(self._htf.keys())
        self.symbols.update_active(list(self._initialised_symbols))
        logger.info(
            f"{len(self._initialised_symbols)}/{len(trade_symbols)} "
            f"symbol(s) ready — trading active")

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

            try:
                htf_data, mtf_data, ltf_data = await asyncio.wait_for(
                    asyncio.gather(
                        self.client.get_candles(symbol, config.HTF_GRANULARITY, htf_bars),
                        self.client.get_candles(symbol, mtf_gran,               mtf_bars),
                        self.client.get_candles(symbol, ltf_gran,               ltf_bars),
                        return_exceptions=True,
                    ),
                    timeout=15,
                )
            except asyncio.TimeoutError:
                logger.warning(f"{symbol}: get_candles timed out after 15s — skipping")
                return

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

            try:
                await asyncio.wait_for(
                    self.client.subscribe_ticks(
                        symbol,
                        lambda tick, s=symbol: self._on_tick(s, tick)),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"{symbol}: subscribe_ticks timed out — "
                    f"proceeding without live tick stream")

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

    # ── Main loop — continuous, non-blocking ───────────────────────────────────

    async def _main_loop(self):
        scan_sleep   = getattr(config, "SCAN_CYCLE_SLEEP", 1)
        cycle_number = 0

        while True:
            cycle_start = time.time()

            # 1. UTC day rollover → reset session
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
                await asyncio.sleep(scan_sleep)
                continue

            cycle_number += 1

            # 2. Refresh active list every cycle in case new symbols initialised,
            #    then get queue of ready symbols
            if self._htf:
                self.symbols.update_active(list(self._htf.keys()))

            queue = self.symbols.get_queue(list(self._initialised_symbols))
            logger.info(
                f"CYCLE {cycle_number} | Queue:{len(queue)} | "
                f"Active:{len(self._htf)} initialised")

            # 3. Scan all queued symbols in parallel — never wait for settles
            raw_results = await asyncio.gather(
                *[self._scan(s) for s in queue],
                return_exceptions=True,
            )

            # 4. Collect all non-None signals
            signals = [r for r in raw_results if isinstance(r, ScanResult)]

            # 5. Filter out symbols that can't trade right now
            signals = [r for r in signals if self.symbols.can_trade_now(r.symbol)]

            # 6. Rank by score*0.85 + win_rate*0.15
            for r in signals:
                r.score = self._composite_score(r.sig, r.symbol)

            # 7. Remove duplicates — keep highest scored per symbol
            best_per_symbol: Dict[str, ScanResult] = {}
            for r in signals:
                if (r.symbol not in best_per_symbol
                        or r.score > best_per_symbol[r.symbol].score):
                    best_per_symbol[r.symbol] = r

            ranked: List[ScanResult] = sorted(
                best_per_symbol.values(), key=lambda r: r.score, reverse=True)

            # 8. Execute top N where N = current_concurrent_limit,
            #    gated only by concurrent-slot availability
            concurrent_limit = self.risk.current_concurrent_limit
            open_count       = len(self._open_contracts)
            available_slots  = max(0, concurrent_limit - open_count)

            top = ranked[:available_slots] if (
                available_slots > 0 and not self._confirmed_paused) else []

            # 9. Execute all top signals in parallel
            if top:
                await asyncio.gather(
                    *[self._execute(r.symbol, r.sig) for r in top],
                    return_exceptions=True,
                )

            # 10. Cycle log
            streak = self.risk.current_streak
            logger.info(
                f"CYCLE {cycle_number} | "
                f"Queue:{len(queue)} | "
                f"Signals:{len(signals)} | "
                f"Executing:{len(top)} | "
                f"Open:{len(self._open_contracts)} | "
                f"Balance:${self.client.balance:.4f} | "
                f"Streak:{'+' if streak >= 0 else ''}{streak}")

            elapsed   = time.time() - cycle_start
            remainder = max(0.0, scan_sleep - elapsed)
            await asyncio.sleep(remainder if remainder > 0 else 0.01)

    # ── Per-symbol scan ────────────────────────────────────────────────────────

    async def _scan(self, symbol: str) -> Optional[ScanResult]:
        try:
            builder = self._ltf.get(symbol)
            if builder is None:
                return None

            ltf_bars = builder.completed_bars

            sig = self.signal.evaluate(ltf_bars, symbol)
            if sig is None or getattr(sig, "direction", "NONE") == "NONE":
                return None

            price = float(ltf_bars[-1].close) if ltf_bars else 0.0

            return ScanResult(
                symbol  = symbol,
                sig     = sig,
                price   = price,
                smc_ctx = SMCContext(),
                score   = getattr(sig, "score", 0.0),
            )

        except Exception as exc:
            logger.error(f"SCAN ERROR {symbol}: {type(exc).__name__}: {exc}", exc_info=True)
            return None

    # ── Execution ──────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult) -> bool:
        # Hard gate — re-verify right before placing the order
        if not self.symbols.can_trade_now(symbol):
            return False

        stake     = await self.risk.calculate_stake()
        direction = sig.direction

        record_signal(
            symbol    = symbol,
            direction = direction,
            strategy  = getattr(sig, "strategy", "unknown"),
            score     = getattr(sig, "score",    0.0),
        )

        buy_resp = await self.client.buy_contract(
            symbol      = symbol,
            direction   = direction,
            stake       = stake,
            multiplier  = getattr(sig, "multiplier",  None),
            stop_loss   = getattr(sig, "stop_loss",   None),
            take_profit = getattr(sig, "take_profit", None),
        )

        if buy_resp is None:
            logger.warning(f"PLACEMENT FAILED: {symbol}")
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

        cid        = str(buy_resp.get("contract_id", ""))
        bal_before = self.client.balance
        buy_price  = float(buy_resp.get("buy_price", stake))

        rec = self.risk.register_open(
            symbol      = symbol,
            direction   = direction,
            stake       = stake,
            entry_price = buy_price,
        )

        try:
            self.journal.open_trade(
                contract_id    = cid,
                symbol         = symbol,
                direction      = direction,
                stake          = stake,
                entry_price    = buy_price,
                balance_before = bal_before,
                asset_class    = get_symbol_class(symbol),
                htf_bias       = "NEUTRAL",
                smc_structure  = "NONE",
                m1             = getattr(sig, "m1_signal", 0),
                m2             = getattr(sig, "m2_signal", 0),
                m3             = getattr(sig, "m3_signal", getattr(sig, "strength", 0)),
                modules        = getattr(sig, "strength", 0),
            )
        except Exception:
            pass

        self._open_contracts[cid] = {
            "symbol":      symbol,
            "direction":   direction,
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
        self._contract_open_times[cid] = time.time()

        self.symbols.record_trade_placed(symbol)
        set_active_trades(len(self._open_contracts))

        logger.info(
            f"▶ {direction} {symbol} | ${stake:.2f} | "
            f"x{getattr(sig, 'multiplier', '?')} | "
            f"SL={getattr(sig, 'stop_loss', '?')} "
            f"TP={getattr(sig, 'take_profit', '?')} | "
            f"streak={self.risk.current_streak}")

        try:
            await self.client.subscribe_contract(
                cid,
                lambda msg, _cid=cid: asyncio.create_task(
                    self._on_contract_result(_cid, msg)))
        except Exception as exc:
            logger.warning(f"subscribe_contract({cid}): {exc}")

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

        info = self._open_contracts.pop(cid, None)
        self._contract_open_times.pop(cid, None)
        if not info:
            return

        symbol      = info["symbol"]
        stake       = info["stake"]
        rec         = info.get("rec")

        sell_price = float(poc.get("sell_price", 0))
        payout     = float(poc.get("payout",     sell_price))
        pnl        = float(poc.get("profit",     0))
        won        = pnl > 0

        try:
            self.journal.close_trade(
                contract_id   = cid,
                exit_price    = sell_price,
                pnl           = pnl,
                payout        = payout,
                balance_after = self.client.balance,
            )
        except Exception:
            pass

        self.symbols.record_contract_closed(symbol)
        self.symbols.record_result(symbol, won=won)
        try:
            self.risk.register_close(rec, exit_price=sell_price, pnl=pnl)
        except Exception:
            pass

        if pnl < 0:
            self._confirmed_daily_loss += abs(pnl)
        self._check_confirmed_loss_limit()

        set_active_trades(len(self._open_contracts))
        update_status(streak=self.risk.current_streak)

        record_trade(
            symbol        = symbol,
            direction     = info.get("direction", "?"),
            stake         = stake,
            pnl           = pnl,
            balance_after = self.client.balance,
            won           = won,
            strategy      = info.get("strategy", ""),
            multiplier    = info.get("multiplier", None),
            close_reason  = "normal",
        )

        try:
            self._push_dashboard()
        except Exception:
            pass

        streak = self.risk.current_streak
        logger.info(
            f"{'✅ WIN' if won else '❌ LOSS'} | "
            f"{symbol} | {info.get('strategy', '')} | "
            f"pnl=${pnl:+.4f} | "
            f"balance=${self.client.balance:.4f} | "
            f"streak={streak}")

    # ── Settle loop — independent of the scan loop ─────────────────────────────

    async def _settle_loop(self):
        settle_wait    = getattr(config, "SETTLE_WAIT_SECS", 15)
        redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)

        while True:
            try:
                await asyncio.sleep(settle_wait)

                await self._handle_orphans()
                self._check_confirmed_loss_limit()

                self._cycle_count += 1
                if self._cycle_count >= redeploy_every:
                    n_open = len(self._open_contracts)
                    logger.info(
                        f"REDEPLOY TRIGGERED: draining {n_open} open "
                        f"contract(s) before restart")
                    while self._open_contracts:
                        logger.info(
                            f"Draining — {len(self._open_contracts)} "
                            f"contract(s) open")
                        await asyncio.sleep(5)
                    restart_scheduler.trigger_redeploy()
                    logger.info("Redeploy triggered — standing by")
                    self._cycle_count = 0

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"_settle_loop: {exc}")

    # ── Orphan handling ─────────────────────────────────────────────────────────

    async def _handle_orphans(self):
        now = time.time()

        for cid, info in list(self._open_contracts.items()):
            opened_at = info.get("opened_at", now)
            age       = now - opened_at

            if age >= CONTRACT_FORCE_CLOSE_SECS:
                symbol = info.get("symbol", "UNKNOWN")
                stake  = info.get("stake", 0.0)
                rec    = info.get("rec")

                try:
                    self.symbols.record_result(symbol, won=False)
                except Exception:
                    pass
                try:
                    self.risk.register_close(rec, exit_price=0, pnl=-stake)
                except Exception:
                    pass
                self.symbols.record_contract_closed(symbol)

                self._open_contracts.pop(cid, None)
                self._contract_open_times.pop(cid, None)
                set_active_trades(len(self._open_contracts))

                try:
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
                except Exception:
                    pass

                try:
                    self._push_dashboard()
                except Exception:
                    pass

                logger.error(f"ORPHAN LOSS: {cid} -{stake}")

            elif age >= CONTRACT_MAX_AGE_SECS:
                try:
                    await self.client.force_check_contract(cid)
                except Exception as exc:
                    logger.warning(f"force_check_contract({cid}): {exc}")
