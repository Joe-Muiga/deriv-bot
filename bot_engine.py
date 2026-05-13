"""
bot_engine.py – SIFM parallel orchestrator, symbol-filtered edition.

Root causes fixed vs previous version
---------------------------------------
1. R_* Volatility Indices removed – these are random-walk instruments.
   SMC has zero edge on them. Every loss on R_25/R_100 was pure noise.
2. 3/3 modules hardcoded – not read from config, cannot be overridden.
3. Cooldown reduced to 60 s per symbol – more trades per session.
4. Only two absolute hard stops: balance<=0, 90% daily loss limit.
5. Stale contract auto-release – open_count never gets stuck.
6. buy_contract retried once before giving up.

SMC-eligible synthetics: BOOM500, BOOM1000, CRASH500, CRASH1000, stpRNG.
Forex pairs (frx*) and crypto (cry*) are always included.
R_*, 1HZ* are excluded entirely.
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
STALE_FACTOR          = 3   # release contract after N × TRADE_DURATION minutes


# ── Symbol helpers ────────────────────────────────────────────────────────────

def _is_forex(s: str) -> bool:
    return s.startswith("frx")

def _ltf_granularity(s: str) -> int:
    return (config.LTF_GRANULARITY_FOREX
            if _is_forex(s)
            else config.LTF_GRANULARITY_SYNTHETIC)

def _ltf_bars(s: str) -> int:
    return (config.LTF_BARS_FOREX
            if _is_forex(s)
            else config.LTF_BARS_SYNTHETIC)

def _is_smc_eligible(symbol: str) -> bool:
    """
    Returns True for instruments where SMC has a genuine edge.
    Excludes pure random-walk Volatility Indices (R_*, 1HZ*).
    """
    # Always exclude random-walk prefixes
    for prefix in config.EXCLUDED_PREFIXES:
        if symbol.startswith(prefix):
            return False
    # Forex and crypto always included
    if symbol.startswith("frx") or symbol.startswith("cry"):
        return True
    # Specific synthetic instruments with trending structure
    for eligible in config.SMC_ELIGIBLE_SYNTHETICS:
        if eligible in symbol:
            return True
    return False


# ── BotEngine ─────────────────────────────────────────────────────────────────

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
        self.smc    = SMCAnalyzer(ob_expiry_bars=config.OB_EXPIRY_BARS)
        self.signal = SignalEngine(
            min_modules = 3,   # HARDCODED – never 2
            min_votes   = 5,   # HARDCODED – never 4
        )
        self.news    = NewsFilter(block_minutes=config.NEWS_BLOCK_MINUTES)
        self.journal = TradeJournal()
        self.symbols = SymbolManager()

        self._htf: Dict[str, CandlestickBuilder] = {}
        self._ltf: Dict[str, CandlestickBuilder] = {}

        self._initializing: Set[str] = set()
        self._init_sem = asyncio.Semaphore(config.INIT_BATCH_SIZE)

        # contract_id → dict with trade info + opened_at timestamp
        self._open_contracts: Dict[str, dict] = {}

        self._queue: List[str] = []

        # Per-symbol cooldown: symbol → time.time() when last trade opened
        self._last_traded_time: Dict[str, float] = {}

        # Dashboard counters only – never used to gate trades
        self._daily_trades:       int = 0
        self._consecutive_losses: int = 0
        self._consecutive_wins:   int = 0
        self._current_day:        str = ""

        self._last_symbol_refresh = 0.0

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=" * 60)
        logger.info("  SIFM Bot  –  SMC-filtered, no-block edition")
        logger.info(f"  Min modules : 3/3 (hardcoded)")
        logger.info(f"  Min votes   : 5/7 (hardcoded)")
        logger.info(f"  Loss limit  : {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% daily drawdown")
        logger.info(f"  Cooldown    : {config.MIN_SECONDS_BETWEEN_SAME_SYMBOL}s per symbol")
        logger.info("=" * 60)

        self._reset_day_if_needed()
        self.client.on_balance(self._on_balance)

        ws_task = asyncio.create_task(self.client.connect())
        for _ in range(60):
            if self.client.is_connected:
                break
            await asyncio.sleep(1)

        if not self.client.is_connected:
            logger.error("Failed to connect within 60 s")
            ws_task.cancel()
            return

        self.risk.set_balance(self.client.balance)
        logger.info(f"Connected | balance=${self.client.balance:.4f}")

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

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_balance(self, balance: float):
        self.risk.set_balance(balance)
        logger.info(f"Balance: ${balance:.4f}")

    def _reset_day_if_needed(self):
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._current_day:
            self._current_day        = today
            self._daily_trades       = 0
            self._consecutive_losses = 0
            self._consecutive_wins   = 0
            logger.info(f"New UTC day: {today}")

    # ── Stale contract cleanup ────────────────────────────────────────────────

    def _release_stale_contracts(self):
        cutoff = time.time() - (config.TRADE_DURATION * 60 * STALE_FACTOR)
        for cid in [c for c, i in self._open_contracts.items()
                    if i["opened_at"] < cutoff]:
            info = self._open_contracts.pop(cid)
            self.risk._open_trade_count = max(0, self.risk._open_trade_count - 1)
            logger.warning(
                f"Stale contract released: {cid} [{info['symbol']}] | "
                f"open={self.risk._open_trade_count}"
            )

    # ── Symbol management ─────────────────────────────────────────────────────

    async def _refresh_symbols(self):
        try:
            active_raw  = await self.client.get_active_symbols()
            active_syms = [
                s["symbol"] for s in active_raw
                if s.get("exchange_is_open", 0) == 1 or
                   any(s["symbol"].startswith(p)
                       for p in ("BOOM", "CRASH", "stpRNG", "frx", "cry",
                                  "JD", "RDBEAR", "RDBULL"))
            ]
            # Filter to SMC-eligible only
            active_syms = [s for s in active_syms if _is_smc_eligible(s)]
            self.symbols.update_active(active_syms)
            self._queue = self.symbols.get_queue(
                max_symbols=config.MAX_SYMBOLS_PER_QUEUE)
            # Extra safety: strip any R_* that slipped through
            self._queue = [s for s in self._queue if _is_smc_eligible(s)]
            self._last_symbol_refresh = time.time()
            logger.info(
                f"Queue: {len(self._queue)} SMC-eligible symbols | "
                f"{self._queue[:10]} … | "
                f"session: {self.symbols.current_session}"
            )
        except Exception as exc:
            logger.error(f"_refresh_symbols: {exc}")

    async def _ensure_all_initialized(self):
        pending = [s for s in self._queue
                   if s not in self._htf and s not in self._initializing]
        if pending:
            await asyncio.gather(
                *[self._init_data(s) for s in pending],
                return_exceptions=True)

    async def _init_data(self, symbol: str):
        if symbol in self._initializing or symbol in self._htf:
            return
        if not _is_smc_eligible(symbol):
            return
        self._initializing.add(symbol)
        async with self._init_sem:
            try:
                gran = _ltf_granularity(symbol)
                bars = _ltf_bars(symbol)
                htf_b = CandlestickBuilder(
                    granularity=config.HTF_GRANULARITY,
                    max_bars=config.HTF_BARS + 20)
                ltf_b = CandlestickBuilder(
                    granularity=gran,
                    max_bars=bars + 20)

                htf_data, ltf_data = await asyncio.gather(
                    self.client.get_candles(
                        symbol, config.HTF_GRANULARITY, config.HTF_BARS),
                    self.client.get_candles(symbol, gran, bars),
                    return_exceptions=True)

                if isinstance(htf_data, Exception): htf_data = []
                if isinstance(ltf_data, Exception): ltf_data = []
                if not htf_data and not ltf_data:
                    logger.warning(f"No data for {symbol}")
                    return

                if htf_data: htf_b.seed(htf_data)
                if ltf_data: ltf_b.seed(ltf_data)

                self._htf[symbol] = htf_b
                self._ltf[symbol] = ltf_b

                await self.client.subscribe_ticks(
                    symbol, lambda t, s=symbol: self._on_tick(s, t))

                logger.info(
                    f"{symbol} ready | htf={htf_b.count}×1h | "
                    f"ltf={ltf_b.count}×{'15m' if _is_forex(symbol) else '1m'}"
                )
            except Exception as exc:
                logger.error(f"_init_data [{symbol}]: {exc}")
            finally:
                self._initializing.discard(symbol)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    async def _dashboard_loop(self):
        while True:
            try:
                self._push_dashboard()
            except Exception:
                pass
            await asyncio.sleep(DASHBOARD_PUSH_EVERY)

    def _push_dashboard(self):
        s = self.risk.summary()
        j = self.journal.session_summary()
        update_status(
            running=True,
            balance=self.client.balance,
            day_start_balance=self.risk.day_start_balance,
            daily_loss_limit_pct=f"{config.DAILY_LOSS_LIMIT_PCT*100:.0f}%",
            paused_for_loss_limit=self.risk.is_paused,
            daily_trades=self._daily_trades,
            consecutive_losses=self._consecutive_losses,
            consecutive_wins=self._consecutive_wins,
            trades_today=s["total_trades"],
            wins_today=s["wins"],
            losses_today=s["losses"],
            win_rate=s["win_rate"],
            open_trades=s["open_trades"],
            session=self.symbols.current_session,
            tradeable_count=len(self._queue),
            gross_profit=j.get("gross_profit", 0),
            gross_loss=j.get("gross_loss", 0),
            profit_factor=j.get("profit_factor", 0),
            best_trade=j.get("best_trade", 0),
            worst_trade=j.get("worst_trade", 0),
            recent_trades=self.journal.recent_trades(20),
            best_symbols=self.symbols.best_symbols(10),
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _main_loop(self):
        while True:
            cycle_start = time.time()
            self._reset_day_if_needed()
            self._release_stale_contracts()

            if time.time() - self._last_symbol_refresh > SYMBOL_REFRESH_EVERY:
                await self._refresh_symbols()

            # Hard stop 1: 90% daily loss limit
            if self.risk.is_paused:
                logger.info(
                    f"⛔ 90% loss limit hit | "
                    f"resumes in ~{self.risk.minutes_until_midnight():.0f} min"
                )
                await asyncio.sleep(60)
                continue

            if not self._queue:
                await asyncio.sleep(30)
                await self._refresh_symbols()
                continue

            await self._ensure_all_initialized()

            candidates = await self._scan_all_parallel()
            candidates.sort(key=lambda x: x[4], reverse=True)

            logger.info(
                f"Cycle | signals={len(candidates)} | "
                f"open={self.risk._open_trade_count} | "
                f"balance=${self.client.balance:.4f}"
            )

            if candidates:
                top = candidates[0]
                logger.info(
                    f"🏆 [{top[0]}] {top[1].direction} "
                    f"score={top[4]:.4f} | {top[1].reason}"
                )

            for sym, sig, price, smc_ctx, score in candidates:
                # Hard stop 2: no funds
                if self.client.balance <= 0:
                    logger.warning("Balance zero – stopping")
                    break
                await self._execute(sym, sig, price, smc_ctx, score)

            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0.1, SCAN_INTERVAL - elapsed))

    # ── Parallel scan ─────────────────────────────────────────────────────────

    async def _scan_all_parallel(self) -> List[Tuple]:
        ready = [s for s in self._queue if s in self._htf]
        if not ready:
            return []
        results = await asyncio.gather(
            *[self._scan_for_signal(s) for s in ready],
            return_exceptions=True)
        return [r for r in results
                if r is not None and not isinstance(r, Exception)]

    # ── Single-symbol evaluation ──────────────────────────────────────────────

    async def _scan_for_signal(self, symbol: str) -> Optional[Tuple]:
        # Safety: never trade random-walk instruments
        if not _is_smc_eligible(symbol):
            return None

        htf = self._htf.get(symbol)
        ltf = self._ltf.get(symbol)
        if not htf or not ltf or htf.count < 20 or ltf.count < 30:
            return None

        # Gate 1: per-symbol cooldown (60 s)
        if time.time() - self._last_traded_time.get(symbol, 0) \
                < config.MIN_SECONDS_BETWEEN_SAME_SYMBOL:
            return None

        # Gate 2: HTF SMC structure
        htf_bars = htf.completed_bars
        H = np.array([b.high  for b in htf_bars])
        L = np.array([b.low   for b in htf_bars])
        C = np.array([b.close for b in htf_bars])
        ha      = ind.atr(H, L, C, 14)
        valid   = ha[~np.isnan(ha)]
        htf_atr = float(valid[-1]) if len(valid) else 0.0

        smc_ctx = self.smc.analyse(htf_bars, htf_atr)
        if smc_ctx.bias == "NEUTRAL":
            return None

        # Gate 3: price inside SMC zone
        ltf_list = ltf.completed_bars
        if not ltf_list:
            return None
        price = float(ltf_list[-1].close)
        if not self.smc.price_in_smc_zone(price, smc_ctx.bias, smc_ctx):
            return None

        # Gate 4: 3/3 modules + 5/7 votes (hardcoded)
        sig = self.signal.evaluate(
            ltf_bars    = ltf_list,
            htf_bias    = smc_ctx.bias,
            smc_ctx     = smc_ctx,
            in_zone     = True,
            min_modules = 3,
            min_votes   = 5,
        )
        if sig.direction == "NONE":
            return None

        # Gate 5: news filter
        if self.news.is_blocked(symbol):
            return None

        # Gate 6: volatility filter
        lH = np.array([b.high  for b in ltf_list])
        lL = np.array([b.low   for b in ltf_list])
        lC = np.array([b.close for b in ltf_list])
        la     = ind.atr(lH, lL, lC, 14)
        valid2 = la[~np.isnan(la)]
        ltf_atr = float(valid2[-1]) if len(valid2) else 0.0
        if htf_atr > 0 and ltf_atr > 2 * htf_atr:
            return None

        trend = min(getattr(smc_ctx, "trend_strength", 0.5), 1.0)
        score = round(sig.probability_score * 0.70 + trend * 0.30, 4)
        return (symbol, sig, price, smc_ctx, score)

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute(self, symbol: str, sig: SignalResult,
                       price: float, smc_ctx, score: float = 0.0):
        stake = self.risk.calculate_stake()
        if stake < config.MIN_STAKE:
            logger.warning(
                f"Stake ${stake:.2f} < MIN_STAKE ${config.MIN_STAKE} – skip {symbol}")
            return

        logger.info(
            f"▶ {sig.direction} {symbol} | stake=${stake:.2f} | "
            f"score={score:.4f} | 3/3 modules | "
            f"balance=${self.client.balance:.4f}"
        )

        buy_resp = None
        for attempt in range(2):
            buy_resp = await self.client.buy_contract(
                symbol    = symbol,
                direction = sig.direction,
                stake     = stake,
                duration  = config.TRADE_DURATION,
                dur_unit  = config.TRADE_DURATION_UNIT,
            )
            if buy_resp:
                break
            logger.warning(f"buy_contract attempt {attempt+1} failed [{symbol}]")
            await asyncio.sleep(1)

        if not buy_resp:
            logger.error(f"Order failed after 2 attempts: {symbol}")
            return

        cid   = str(buy_resp.get("contract_id", ""))
        bal_b = self.client.balance
        ac    = get_symbol_class(symbol)

        rec = self.risk.register_open(
            symbol=symbol, direction=sig.direction,
            stake=stake, entry_price=price)

        self.journal.open_trade(
            contract_id=cid, symbol=symbol, direction=sig.direction,
            stake=stake, entry_price=price, balance_before=bal_b,
            asset_class=ac, htf_bias=smc_ctx.bias,
            smc_structure=smc_ctx.structure,
            m1=sig.m1_signal, m2=sig.m2_signal,
            m3=sig.m3_signal, modules=sig.strength)

        self._open_contracts[cid] = dict(
            symbol=symbol, direction=sig.direction,
            stake=stake, entry_price=price,
            rec=rec, opened_at=time.time())

        # Cooldown starts at open, regardless of outcome
        self._last_traded_time[symbol] = time.time()

        await self.client.subscribe_contract(
            cid,
            lambda msg, _c=cid: asyncio.create_task(
                self._on_contract_result(_c, msg)))

        logger.info(
            f"✔ Opened {cid} | {symbol} {sig.direction} | "
            f"open={self.risk._open_trade_count}"
        )

    # ── Contract result ───────────────────────────────────────────────────────

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

        won = pnl > 0
        self.risk.register_close(info["rec"], exit_price=sell_price, pnl=pnl)
        self.journal.close_trade(
            contract_id=cid, exit_price=sell_price,
            pnl=pnl, payout=payout, balance_after=bal_after)
        self.symbols.record_trade(info["symbol"], won=won, pnl=pnl)

        self._daily_trades += 1
        if won:
            self._consecutive_wins  += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins   = 0

        logger.info(
            f"{'✅ WIN' if won else '❌ LOSS'} | "
            f"{info['symbol']} {info['direction']} | "
            f"pnl=${pnl:+.4f} | balance=${bal_after:.4f} | "
            f"open={self.risk._open_trade_count} | "
            f"W{self._consecutive_wins}/L{self._consecutive_losses}"
        )

    # ── Tick handler ─────────────────────────────────────────────────────────

    def _on_tick(self, symbol: str, tick: dict):
        epoch = int(tick.get("epoch", time.time()))
        price = float(tick.get("quote", 0))
        if not price:
            return
        if symbol in self._ltf:
            self._ltf[symbol].add_tick(epoch, price)
        if symbol in self._htf:
            self._htf[symbol].add_tick(epoch, price)
