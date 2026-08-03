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
import math
import random
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Set, Tuple

import numpy as np

import config
import exit_engine
import restart_scheduler
import symbols as sym_module
import strategy_stats
import meta_labeling
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
                        record_trade,
                        record_signal, record_failure,
                        update_open_contracts, _status)
from symbols import get_symbol_class

logger = logging.getLogger(__name__)

DASHBOARD_PUSH_EVERY  = 5        # push every 5 seconds
INIT_BATCH_SIZE       = getattr(config, "INIT_BATCH_SIZE", 10)

CONTRACT_MAX_AGE_SECS     = getattr(config, "CONTRACT_MAX_AGE_SECS",      900)
CONTRACT_FORCE_CLOSE_SECS = getattr(config, "CONTRACT_FORCE_CLOSE_SECS", 1350)

# ── Reconciliation (Fix C) ───────────────────────────────────────────────
RECONCILE_POLL_INTERVAL_SECS = getattr(config, "RECONCILE_POLL_INTERVAL_SECS", 30)
RECONCILE_MAX_SECS           = getattr(config, "RECONCILE_MAX_SECS",           1800)

# ── Multiplier max-hold (Fix E) ──────────────────────────────────────────
MULTIPLIER_MAX_HOLD_SECS = getattr(config, "MULTIPLIER_MAX_HOLD_MINS", 30) * 60

# ── Redeploy drain (Fix G) ───────────────────────────────────────────────
DRAIN_MAX_SECS = getattr(config, "DRAIN_MAX_SECS", 1800)


# ─── Result container ──────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    symbol:    str
    sig:       SignalResult
    price:     float
    smc_ctx:   SMCContext
    score:     float = 0.0
    rank_key:  float = 0.0   # score * bandit priority weight — sort key only,
                              # never persisted/logged in place of `score`


# ─── Thompson sampling bandit ───────────────────────────────────────────────

class _ThompsonBandit:
    """
    Beta(alpha, beta) posterior per (strategy, symbol) pair, used ONLY to
    weight execution priority when multiple signals compete for the same
    cycle's slots — it never touches the composite score itself.

    Deliberately stateless / derived-on-read from strategy_stats rather
    than kept as a running in-memory counter: _settle_loop triggers a
    Render redeploy every config.REDEPLOY_EVERY_N_CYCLES settle ticks,
    which wipes process memory. An in-memory posterior would reset to a
    flat Beta(1,1) prior on every one of those restarts and effectively
    never learn anything. Reading wins/losses straight out of
    strategy_stats' persisted backend means the posterior survives
    redeploys (as long as the underlying SQLite/JSON file sits on a
    disk that itself survives redeploys — see strategy_stats.py's own
    persistence caveat).
    """

    def __init__(self, window: int = strategy_stats.DEFAULT_WINDOW):
        self._window = window

    def sample(self, strategy: str, symbol: str) -> float:
        try:
            rate, _lo, _hi, n = strategy_stats.stats.get_win_rate(
                strategy, symbol, window=self._window)
        except Exception as exc:
            logger.warning(f"bandit.sample({strategy},{symbol}) failed: {exc}")
            return 0.5

        wins   = round(rate * n)
        losses = n - wins
        alpha  = 1.0 + wins    # Beta(1,1) uniform prior with no history
        beta_p = 1.0 + losses

        try:
            return random.betavariate(alpha, beta_p)
        except Exception:
            return 0.5


# ─── Per-strategy expectancy tracker (Implementation Brief v3, task 3) ─────────

class _StrategyExpectancyTracker:
    """
    Per-strategy (MEAN_REVERSION / STEP / JUMP_BUILDUP / TREND_SHIFT /
    BOOM_CRASH / ...) win rate, avg win, avg loss, and profit factor,
    plus a Wilson-score confidence interval on win rate given trade count
    — this is what turns the account-level aggregate the dashboard already
    shows (journal.session_summary()) into something that can actually be
    trusted or distrusted PER STRATEGY, which is the brief's whole point:
    at n=39 trades total, 61.5% isn't yet distinguishable from breakeven.

    Deliberately a separate, self-contained in-memory tracker rather than
    an extension of strategy_stats.py — strategy_stats.py's job (feeding
    the meta-labeling filter and the Thompson bandit) is a different
    consumer of similar data, and this file only has confirmed sight of
    its read API (get_win_rate), not its write/storage internals, so
    bolting new aggregate fields onto it without seeing its source would
    be a guess. Fed from the exact same _apply_settlement() call site that
    already calls strategy_stats.stats.record_trade(), so the two never
    disagree on what a "trade" is. Resets on redeploy along with the rest
    of this process's in-memory state — acceptable here since the ask is
    to surface it, not persist it durably across restarts.
    """

    def __init__(self):
        self._trades: Dict[str, List[Tuple[bool, float]]] = {}

    def record(self, strategy: str, won: bool, pnl: float) -> None:
        self._trades.setdefault(strategy, []).append((won, float(pnl)))

    @staticmethod
    def _wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
        """95% Wilson score interval (z=1.96) on a binomial win rate."""
        if n == 0:
            return (0.0, 1.0)
        phat = wins / n
        denom = 1.0 + (z * z) / n
        center = (phat + (z * z) / (2 * n)) / denom
        margin = (z * math.sqrt((phat * (1 - phat) + (z * z) / (4 * n)) / n)) / denom
        return (max(0.0, center - margin), min(1.0, center + margin))

    def summary(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for strategy, trades in self._trades.items():
            n = len(trades)
            if n == 0:
                continue
            win_pnls  = [pnl for won, pnl in trades if won]
            loss_pnls = [pnl for won, pnl in trades if not won]
            win_count   = len(win_pnls)
            loss_count  = len(loss_pnls)
            gross_win   = sum(win_pnls)
            gross_loss  = abs(sum(loss_pnls))
            avg_win     = (gross_win / win_count) if win_count else 0.0
            avg_loss    = (gross_loss / loss_count) if loss_count else 0.0
            if gross_loss > 0:
                profit_factor = gross_win / gross_loss
            else:
                profit_factor = None if gross_win == 0 else float("inf")
            win_rate = win_count / n
            ci_lo, ci_hi = self._wilson_ci(win_count, n)
            out[strategy] = {
                "trades":         n,
                "win_rate_pct":   round(win_rate * 100, 1),
                "win_rate_ci_pct": [round(ci_lo * 100, 1), round(ci_hi * 100, 1)],
                "avg_win":        round(avg_win, 4),
                "avg_loss":       round(avg_loss, 4),
                "profit_factor":  (round(profit_factor, 3)
                                    if profit_factor is not None and math.isfinite(profit_factor)
                                    else profit_factor),
                "gross_profit":   round(gross_win, 4),
                "gross_loss":     round(gross_loss, 4),
            }
        return out


# ─── Bot engine ────────────────────────────────────────────────────────────────

class BotEngine:

    def __init__(self):
        self._cycle_count:            int                = 0
        self._running:                bool               = False
        self._session_start_balance:  float              = 0.0
        self._open_contracts:         dict               = {}
        self._contract_open_times:    dict               = {}

        # ── Contracts past CONTRACT_FORCE_CLOSE_SECS that still haven't
        #    settled for real — actively re-polled on RECONCILE_POLL_
        #    INTERVAL_SECS rather than ever being fabricated (Fix C).
        #    {contract_id: {"reconcile_started_at": epoch, "last_poll": epoch}}
        self._reconciling:            dict               = {}

        self._htf:                    Dict[str, CandlestickBuilder] = {}
        self._mtf:                    Dict[str, CandlestickBuilder] = {}
        self._ltf:                    Dict[str, CandlestickBuilder] = {}

        # ── Raw tick buffer — feeds evaluate(ticks=...) for the tick-based
        #    evaluators (digit parity, drift fade, jump buildup, trend
        #    shift), which previously always saw ticks=None because
        #    nothing ever populated or passed a tick buffer.
        self._raw_ticks:              Dict[str, Deque[dict]] = {}

        self._initialised_symbols:    Set[str]           = set()
        self._initializing:           Set[str]           = set()

        # ── Symbols whose tick subscription failed during _init_data —
        #    candles/data are seeded but no live ticks are arriving, so
        #    these are ready-but-frozen until re-subscribed.
        self._tick_degraded:          Set[str]           = set()

        # ── Consecutive identical buy-placement failures per symbol —
        #    drives the circuit breaker in _execute().
        self._buy_failure_streak:     Dict[str, int]     = {}

        self._confirmed_daily_loss:   float              = 0.0
        self._day_start_balance:      float              = 0.0
        self._confirmed_paused:       bool               = False
        self._current_utc_day:        int                = -1

        # ── Ensemble voting: per-symbol rolling history of (strategy,
        #    direction, timestamp) tuples for every signal produced by
        #    _scan, pruned to config.ENSEMBLE_AGREEMENT_WINDOW_SECS.
        self._ensemble_history:       Dict[str, Deque[Tuple[str, str, float]]] = {}

        # ── Thompson sampling bandit — see _ThompsonBandit docstring
        #    for why this reads through strategy_stats instead of
        #    keeping its own in-memory posterior.
        self._bandit = _ThompsonBandit()

        # ── Per-strategy expectancy tracker (Implementation Brief v3,
        #    task 3) — fed in _apply_settlement(), surfaced in
        #    _push_dashboard(). See _StrategyExpectancyTracker docstring.
        self._strategy_expectancy = _StrategyExpectancyTracker()

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

    # ── Ensemble voting ────────────────────────────────────────────────────────

    def _log_ensemble_signal(self, symbol: str, strategy: str, direction: str):
        """Record every signal _scan produces (whether or not it ends up
        trading) so the agreement window has something to check against."""
        window = getattr(config, "ENSEMBLE_AGREEMENT_WINDOW_SECS", 60)
        now    = time.time()
        dq     = self._ensemble_history.setdefault(symbol, deque())
        dq.append((strategy, direction, now))
        while dq and (now - dq[0][2]) > window:
            dq.popleft()

    def _ensemble_agrees(self, symbol: str, direction: str) -> bool:
        """
        True if >= config.ENSEMBLE_MIN_STRATEGIES_AGREEING distinct
        strategies have signalled `direction` on `symbol` within the
        agreement window (including the current signal, since it was
        logged via _log_ensemble_signal before this is called).

        NOTE: config.py's own STRATEGY ROUTING section states every
        traded symbol is routed to exactly one strategy evaluator
        (MEAN_REVERSION_SYMBOLS / STEP_SYMBOLS / etc are disjoint).
        With that routing, at most one strategy will ever signal a
        given symbol in a given cycle, so this will almost always
        return False once ENSEMBLE_MODE=True — see flags in the
        accompanying explanation before enabling it.
        """
        window    = getattr(config, "ENSEMBLE_AGREEMENT_WINDOW_SECS", 60)
        min_agree = getattr(config, "ENSEMBLE_MIN_STRATEGIES_AGREEING", 2)
        now       = time.time()
        dq        = self._ensemble_history.get(symbol)
        if not dq:
            return False
        strategies = {s for (s, d, ts) in dq
                      if d == direction and (now - ts) <= window}
        return len(strategies) >= min_agree

    # ── Session / day-of-week score weighting ─────────────────────────────────

    def _session_dow_weight(self, symbol: str) -> float:
        """
        Implementation Brief v5 / B2.

        Previously: a multiplier from the static config.SESSION_DOW_WEIGHT_TABLE,
        keyed on symbol category (via get_symbol_class). That table was a
        dead no-op in practice — its keys ("BOOM600_CRASH900", "JUMP50",
        etc.) never matched any category get_symbol_class() actually
        returns for the symbols this bot trades, so every lookup silently
        fell through to SESSION_DOW_WEIGHT_DEFAULT=1.0.

        Now: driven by strategy_stats.stats.get_hourly_payout_ratio(),
        which buckets REALIZED win-payout ratio (payout/stake on settled
        winning trades) by UTC hour across this bot's own trade history.
        Independent payout auditing found off-peak/quiet-session payouts
        can run meaningfully below the advertised headline rate — a
        direct hit to the Kelly `b` term regardless of signal quality.
        This replaces a guessed table with the bot's own measured data,
        which is a strictly better version of a feature that was already
        wired in but inert.

        The `symbol` argument is accepted for call-site compatibility
        (and in case a future pass wants to bucket per symbol/category
        as well as per hour) but the weight itself is currently computed
        globally across all settled trades, since B2's underlying finding
        — quiet-hour payout degradation — isn't instrument-specific.

        Falls back to config.SESSION_DOW_WEIGHT_DEFAULT (1.0, neutral)
        whenever there isn't yet enough settled-trade history to trust
        the read, so this behaves as a no-op early on and only starts
        adjusting once real data backs it.
        """
        default = getattr(config, "SESSION_DOW_WEIGHT_DEFAULT", 1.0)
        min_per_hour = getattr(config, "SESSION_HOURLY_MIN_TRADES", 15)
        min_total    = getattr(config, "SESSION_HOURLY_MIN_TOTAL_TRADES", 200)
        weight_min   = getattr(config, "SESSION_HOURLY_WEIGHT_MIN", 0.8)
        weight_max   = getattr(config, "SESSION_HOURLY_WEIGHT_MAX", 1.2)

        try:
            hourly = strategy_stats.stats.get_hourly_payout_ratio()
        except Exception as exc:
            logger.debug(f"SESSION WEIGHT: hourly payout lookup failed ({exc}) — using default")
            return default

        total_n = sum(n for (_ratio, n) in hourly.values())
        if total_n < min_total:
            return default

        now_hour = _dt.datetime.utcnow().hour
        ratio, n = hourly.get(now_hour, (None, 0))
        if ratio is None or n < min_per_hour:
            return default

        trusted_ratios = [r for (r, cnt) in hourly.values() if r is not None and cnt >= min_per_hour]
        if not trusted_ratios:
            return default
        overall_avg = sum(trusted_ratios) / len(trusted_ratios)
        if overall_avg <= 0:
            return default

        weight = ratio / overall_avg
        weight = max(weight_min, min(weight_max, weight))
        return round(weight, 4)

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

        dash_task     = asyncio.create_task(self._dashboard_loop())
        settle_task   = asyncio.create_task(self._settle_loop())
        degraded_task = asyncio.create_task(self._degraded_retry_loop())
        # NOTE: the daily Kenya-midnight redeploy timer (Fix G) is started
        # by main.py's existing restart_scheduler.start_restart_scheduler()
        # call, unchanged — NOT started again here, to avoid two competing
        # scheduler loops. _settle_loop below just reads
        # restart_scheduler.is_redeploy_pending() / calls
        # restart_scheduler.trigger_redeploy().

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.critical(f"Main loop crashed: {exc}\n{traceback.format_exc()}")
        finally:
            dash_task.cancel()
            settle_task.cancel()
            degraded_task.cancel()
            ws_task.cancel()

    # ── Startup — TRADE_SYMBOLS only ───────────────────────────────────────────

    async def _init_all_symbols(self):
        trade_symbols = list(getattr(config, "TRADE_SYMBOLS",
                             getattr(config, "ALL_TRADE_SYMBOLS",
                             getattr(config, "ALL_SYMBOLS", []))))

        logger.info(f"Initialising {len(trade_symbols)} symbols: {trade_symbols}")

        if not trade_symbols:
            logger.error(
                "NO SYMBOLS IN CONFIG — check TRADE_SYMBOLS / "
                "ALL_TRADE_SYMBOLS in config.py")
            return

        # Implementation Brief v5 / A5 — BEAR_BULL_SYMBOLS need more LTF
        # bars than the default config.LTF_BARS=30 provides (TREND_SHIFT's
        # min_bars=51 exceeds the default 30+20=50 builder cap — see
        # config.BEAR_BULL_LTF_BARS for the confirmed bug this fixes).
        # Every other category keeps using the default (ltf_bars=None ->
        # config.LTF_BARS inside _init_data()).
        bear_bull_symbols = set(getattr(config, "BEAR_BULL_SYMBOLS", []))
        bear_bull_ltf_bars = getattr(config, "BEAR_BULL_LTF_BARS", config.LTF_BARS)

        results = await asyncio.gather(
            *[self._init_data(
                s,
                ltf_bars=bear_bull_ltf_bars if s in bear_bull_symbols else None,
              ) for s in trade_symbols],
            return_exceptions=True,
        )
        for s, r in zip(trade_symbols, results):
            if isinstance(r, Exception):
                logger.warning(f"{s}: failed to initialise — skipping ({r})")

        ready = list(self._htf.keys())
        self._initialised_symbols = set(ready)
        self.symbols.update_active(ready)
        logger.info(
            f"Initialised: {len(ready)}/{len(trade_symbols)} "
            f"symbol(s) ready: {ready}")

    # ── Data initialisation — three TFs simultaneously ─────────────────────────

    async def _init_data(
        self,
        symbol:   str,
        htf_bars: int = None,
        mtf_bars: int = None,
        ltf_bars: int = None,
    ):
        logger.info(f"INIT_DATA: starting {symbol}")

        if symbol in self._initializing or symbol in self._htf:
            logger.info(f"INIT_DATA: {symbol} already initializing/initialised — skipping")
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
                self._tick_degraded.discard(symbol)
            except asyncio.TimeoutError:
                logger.warning(
                    f"{symbol}: subscribe_ticks timed out — "
                    f"DEGRADED (no live tick stream, candles frozen at seed) "
                    f"— will retry every {config.TICK_RESUBSCRIBE_RETRY_SECS}s")
                self._tick_degraded.add(symbol)
            except Exception as tick_exc:
                logger.warning(
                    f"{symbol}: subscribe_ticks failed — "
                    f"DEGRADED (no live tick stream, candles frozen at seed) "
                    f"— will retry every {config.TICK_RESUBSCRIBE_RETRY_SECS}s "
                    f"| {tick_exc}")
                self._tick_degraded.add(symbol)

            logger.info(
                f"{symbol}: ready | htf={htf_b.count} | "
                f"mtf={mtf_b.count} | ltf={ltf_b.count} "
                f"(ltf_gran={ltf_gran}s)")
            logger.info(f"INIT_DATA: {symbol} ready — htf={htf_b.count} ltf={ltf_b.count}")

        except Exception as exc:
            logger.error(f"_init_data({symbol}): skipping — {exc}")
        finally:
            self._initializing.discard(symbol)

    def _on_tick(self, symbol: str, tick: dict):
        import time as _t
        epoch = int(tick.get("epoch", int(_t.time())))
        price = float(tick.get("quote", 0))
        if price == 0:
            logger.debug(f"TICK IGNORED: {symbol} — zero/invalid quote in {tick}")
            return

        buf = self._raw_ticks.setdefault(
            symbol, deque(maxlen=config.TICK_BUFFER_MAXLEN))
        buf.append({"epoch": epoch, "quote": price})

        for store in (self._ltf, self._mtf, self._htf):
            if symbol in store:
                store[symbol].add_tick(epoch, price)

        self._tick_degraded.discard(symbol)
        logger.debug(f"TICK: {symbol} epoch={epoch} price={price}")

    # ── Degraded-symbol tick resubscription ─────────────────────────────────────

    async def _degraded_retry_loop(self):
        interval = getattr(config, "TICK_RESUBSCRIBE_RETRY_SECS", 30)
        while True:
            try:
                await asyncio.sleep(interval)
                for symbol in list(self._tick_degraded):
                    logger.info(f"{symbol}: retrying tick subscription (degraded)")
                    try:
                        await asyncio.wait_for(
                            self.client.subscribe_ticks(
                                symbol,
                                lambda tick, s=symbol: self._on_tick(s, tick)),
                            timeout=10,
                        )
                        self._tick_degraded.discard(symbol)
                        logger.info(f"{symbol}: tick subscription recovered")
                    except Exception as exc:
                        logger.warning(
                            f"{symbol}: degraded retry failed — still no "
                            f"live tick stream | {exc}")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"_degraded_retry_loop: {exc}")

    # ── Dashboard ──────────────────────────────────────────────────────────────

    async def _dashboard_loop(self):
        while True:
            try:
                self._push_dashboard()
            except Exception:
                pass
            try:
                self._check_tracking_divergence()
            except Exception:
                pass
            await asyncio.sleep(DASHBOARD_PUSH_EVERY)

    def _check_tracking_divergence(self) -> None:
        """
        Fix D: bot_engine's _open_contracts and deriv_client's own tracking
        sets (_polling_contracts / _subscribed_contracts) must always
        describe the same set of contracts. Once Fix C removes the
        fabrication path this should never happen — treat any occurrence
        as a bug to investigate, not something to silently absorb.
        """
        try:
            tracked_by_client = self.client.get_tracked_contract_ids()
        except Exception:
            return

        ours = set(self._open_contracts.keys())
        # Contracts in reconcile_pending are still legitimately open even
        # though the client-side polling entry for them was already
        # consumed by the mechanism that pushed them into reconciliation —
        # don't flag those as a divergence.
        ours_excl_reconciling = ours - set(self._reconciling.keys())

        missing_from_client = ours_excl_reconciling - tracked_by_client
        if missing_from_client:
            logger.warning(
                f"TRACKING DIVERGENCE: {len(missing_from_client)} contract(s) "
                f"open in bot_engine but not tracked by deriv_client: "
                f"{missing_from_client}"
            )

    def _push_dashboard(self):
        risk_s  = self.risk.summary()
        summary = self.journal.session_summary()

        # Implementation Brief v3, task 3 — per-strategy win rate / avg
        # win / avg loss / profit factor / CI, next to the aggregate stats
        # already pushed below. Written directly onto the shared `_status`
        # dict (imported by reference from keep_alive) rather than passed
        # as a new kwarg to update_status() below, since this file doesn't
        # have sight of update_status()'s exact parameter list and a typo'd
        # kwarg there would raise before any of the existing fields get
        # updated. `_status` is already read/written this way elsewhere in
        # this function (recent_trades, balance_history), so this follows
        # the same established pattern.
        try:
            _status["strategy_expectancy"] = self._strategy_expectancy.summary()
        except Exception as exc:
            logger.warning(f"strategy_expectancy dashboard push failed: {exc}")

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

            # 1. UTC day rollover → daily-loss-limit bookkeeping only.
            #    Per Implementation Brief v2, Requirement 2 / Fix F: the
            #    escalating per-symbol loss-suspension ladder must NOT
            #    reset on this (or any other) calendar boundary — only a
            #    redeploy (real process restart) resets it, which happens
            #    for free since SymbolManager is reconstructed from
            #    scratch on startup. self.symbols.reset_session() is
            #    deliberately no longer called here. The daily-loss-limit
            #    concept below is separate and legitimate — left on its
            #    existing UTC-midnight boundary per the brief's open
            #    question (not moved to Kenya midnight without explicit
            #    sign-off).
            today = _dt.datetime.utcnow().day
            if today != self._current_utc_day:
                self._confirmed_daily_loss = 0.0
                self._day_start_balance    = self.client.balance
                self._current_utc_day      = today
                logger.info(
                    f"UTC day changed → daily-loss-limit reset "
                    f"(symbol suspension ladder NOT reset — redeploy-only) | "
                    f"day_start_balance=${self._day_start_balance:.4f}")

            if restart_scheduler.is_redeploy_pending():
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

            # 4b. Log every raw signal into the ensemble agreement tracker —
            #     unconditionally, so the rolling window has data whether
            #     or not ENSEMBLE_MODE is currently on.
            for r in signals:
                self._log_ensemble_signal(
                    r.symbol, getattr(r.sig, "strategy", "unknown"), r.sig.direction)

            # 5. Filter out symbols that can't trade right now
            signals = [r for r in signals if self.symbols.can_trade_now(r.symbol)]

            # 5b. Ensemble voting gate — require >=N independent strategies
            #     agreeing on direction within the window before a signal
            #     is even eligible to be ranked/executed. No-op when
            #     config.ENSEMBLE_MODE is False (the default).
            if getattr(config, "ENSEMBLE_MODE", False):
                signals = [r for r in signals
                           if self._ensemble_agrees(r.symbol, r.sig.direction)]

            # 6. Rank by score*0.85 + win_rate*0.15
            for r in signals:
                r.score = self._composite_score(r.sig, r.symbol)

            # 6b. Session/day-of-week weighting — multiplier on the final
            #     score itself (not just the ranking order).
            for r in signals:
                r.score = round(r.score * self._session_dow_weight(r.symbol), 4)

            # 7. Remove duplicates — keep highest scored per symbol
            best_per_symbol: Dict[str, ScanResult] = {}
            for r in signals:
                if (r.symbol not in best_per_symbol
                        or r.score > best_per_symbol[r.symbol].score):
                    best_per_symbol[r.symbol] = r

            # 7b. Thompson-sampling bandit — draws a fresh Beta(alpha,beta)
            #     sample per (strategy, symbol) pair each cycle and uses it
            #     to weight *ranking order only* (0.9x-1.1x band, so it
            #     nudges priority among competing signals without
            #     overriding the composite score). r.score itself (already
            #     including session/dow weighting) stays what's logged.
            for r in best_per_symbol.values():
                bandit_sample = self._bandit.sample(
                    getattr(r.sig, "strategy", "unknown"), r.symbol)
                r.rank_key = round(r.score * (0.9 + 0.2 * bandit_sample), 4)

            ranked: List[ScanResult] = sorted(
                best_per_symbol.values(), key=lambda r: r.rank_key, reverse=True)

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
            ticks    = list(self._raw_ticks.get(symbol, ()))

            sig = self.signal.evaluate(ltf_bars, symbol, ticks=ticks)
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

        strategy = getattr(sig, "strategy", "unknown")

        # Implementation Brief v5 / A4 — belt-and-suspenders stats gate.
        # Coarser than the Kelly overlay in calculate_stake() (uses the
        # flat config.STRATEGY_WIN_RATE_FLOOR / STRATEGY_WIN_RATE_MIN_TRADES
        # check rather than full Kelly math), but it's an independent
        # second check with its own clean log line, and it skips the trade
        # entirely rather than just zeroing the stake.
        if strategy_stats.stats.is_underperforming(strategy, symbol):
            logger.info(
                f"STATS SKIP: {symbol} | {strategy} | "
                f"underperforming (win rate below floor)")
            return False

        # Meta-labeling gate — skip execution if the take-trade-or-not
        # filter rejects it. predict_take_trade() has its own internal
        # min-trades gating (config.META_LABEL_MIN_TRADES); below that
        # threshold it always returns (True, 1.0) — a pass-through, not
        # a real prediction.
        try:
            take, confidence = meta_labeling.predict_take_trade({
                "strategy":    strategy,
                "symbol":      symbol,
                "entry_score": float(getattr(sig, "score", 0.0)),
            })
        except Exception as exc:
            logger.warning(
                f"meta_labeling.predict_take_trade({symbol}) failed — "
                f"passing signal through: {exc}")
            take, confidence = True, 1.0

        if not take:
            logger.info(
                f"META-LABEL SKIP: {symbol} | {strategy} | "
                f"P(win)={confidence:.3f} < threshold")
            record_failure(
                symbol    = symbol,
                direction = sig.direction,
                stake     = 0.0,
                strategy  = strategy,
                reason    = f"meta_label_reject(conf={confidence:.3f})",
            )
            return False

        stake     = await self.risk.calculate_stake(strategy=strategy, symbol=symbol)

        # Implementation Brief v5 / A2 — Kelly overlay guard. calculate_stake()
        # now runs the Kelly overlay for this pair (A1 above); once it has
        # >= config.KELLY_MIN_TRADES logged trades and finds no computable
        # edge (b <= 0), it forces stake to $0.00. Without this guard a $0
        # stake would get sent to Deriv as a real buy request, fail the
        # API's minimum-stake check, and trip the buy-failure circuit
        # breaker (see below) for the wrong reason — this is a deliberate
        # skip, not a placement failure.
        if stake <= 0:
            logger.info(
                f"KELLY SKIP: {symbol} | {strategy} | stake=$0.00 — "
                f"no computable edge, trade skipped")
            record_failure(
                symbol    = symbol,
                direction = sig.direction,
                stake     = 0.0,
                strategy  = strategy,
                reason    = "kelly_zero_edge",
            )
            return False

        # NOTE: for DIGIT signals (JUMP_BUILDUP) this is "MATCH"/"DIFFER",
        # not "LONG"/"SHORT" — passed through below to record_signal(),
        # risk.register_open(), and journal.open_trade() purely as a label.
        # Those call sites appear to treat it as opaque metadata rather than
        # branching on the literal value, but this file doesn't have sight
        # of risk_manager.py/trade_journal.py's internals to confirm that
        # with certainty — flagging rather than silently assuming.
        direction = sig.direction

        record_signal(
            symbol    = symbol,
            direction = direction,
            strategy  = getattr(sig, "strategy", "unknown"),
            score     = getattr(sig, "score",    0.0),
        )

        # Implementation Brief v3, finding #3 / task 2: JUMP_BUILDUP fires a
        # digit-contract recommendation (Matches/Differs), not a price
        # direction — build-up confidence has no LONG/SHORT read, jump
        # direction is 50/50 by design. Previously this fell through to the
        # buy_contract() (CALL/PUT) branch below with a hardcoded direction,
        # i.e. blindly betting a coin flip on a sub-1:1 payout every time it
        # fired. sig.contract_kind=="DIGIT" (set by evaluate_jump_buildup())
        # routes it to the real digit-contract path instead — checked before
        # the Multiplier/Rise-Fall split since it's an orthogonal axis (which
        # contract family, not which symbol).
        if getattr(sig, "contract_kind", "RISE_FALL") == "DIGIT":
            digit = getattr(sig, "digit", None)
            match_type = getattr(sig, "match_type", None)
            if digit is None or match_type is None:
                logger.warning(
                    f"PLACEMENT SKIPPED: {symbol} DIGIT signal missing "
                    f"digit/match_type (digit={digit}, match_type={match_type})"
                )
                buy_resp = None
            else:
                buy_resp = await self.client.buy_digit_contract(
                    symbol     = symbol,
                    stake      = stake,
                    digit      = digit,
                    match_type = match_type,
                )
        # Boom/Crash, Jump, and Drift Switch symbols don't support CALL/PUT
        # Rise/Fall on this account — route them to buy_multiplier() instead.
        # config.MULTIPLIER_SYMBOLS is the single source of truth for this
        # split (see config.py's "STRATEGY ROUTING" section); everything
        # else keeps using buy_contract() exactly as before.
        elif symbol in getattr(config, "MULTIPLIER_SYMBOLS", set()):
            buy_resp = await self.client.buy_multiplier(
                symbol   = symbol,
                direction= direction,
                stake    = stake,
                strategy = strategy,
            )
        else:
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

            # ── Circuit breaker — repeated identical buy failures on the
            #    same symbol previously retried every ~3s indefinitely
            #    with the same broken parameters. Suspend after N in a
            #    row so it stops hammering the API on a bug that a
            #    fast retry loop can't fix.
            streak = self._buy_failure_streak.get(symbol, 0) + 1
            self._buy_failure_streak[symbol] = streak
            threshold = config.BUY_FAILURE_CIRCUIT_BREAKER_THRESHOLD
            if streak >= threshold:
                suspend_mins = config.BUY_FAILURE_CIRCUIT_BREAKER_SUSPEND_MINS
                logger.error(
                    f"CIRCUIT BREAKER: {symbol} hit {streak} consecutive "
                    f"buy failures — suspending {suspend_mins}min")
                self.symbols.suspend(symbol, suspend_mins)
                self._buy_failure_streak[symbol] = 0

            try:
                self._push_dashboard()
            except Exception:
                pass
            return False

        self._buy_failure_streak[symbol] = 0

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

        # ── Adaptive Exit Engine — tighter-cadence per-contract monitor for
        #    Multiplier contracts only (open-ended risk; Rise/Fall keeps its
        #    existing fixed-expiry handling untouched). Purely additive: does
        #    not change how the contract above was registered.
        if (symbol in getattr(config, "MULTIPLIER_SYMBOLS", set())
                and getattr(config, "EXIT_ENGINE_ENABLED", False)
                and symbol in getattr(config, "EXIT_ENGINE_SYMBOLS", set())):
            asyncio.create_task(self._monitor_exit(cid))

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

    # ── Shared settlement interpretation (Fix C.1, Requirement 1) ──────────────
    #
    # ONE implementation of "how do we turn a real Deriv settlement response
    # into a recorded win/loss", used by _on_contract_result() (WS push /
    # immediate poll), the reconciliation path in _handle_orphans(), and the
    # Multiplier max-hold close path. Must NEVER be called with a fabricated
    # or guessed `poc` — every call site is required to have a real
    # is_sold/is_expired response (or a real sell/profit_table response
    # reshaped into the same field names) before calling this.

    async def _apply_settlement(self, cid: str, info: dict, poc: dict,
                                 close_reason: str = "normal") -> None:
        symbol = info["symbol"]
        stake  = info["stake"]
        rec    = info.get("rec")

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

        strategy    = info.get("strategy", "unknown")
        entry_score = float(getattr(info.get("sig"), "score", 0.0))

        # Feed strategy_stats — this is the source of truth the
        # meta-labeling filter and the Thompson bandit both read from,
        # so it has to be populated for either of those to do anything.
        try:
            strategy_stats.stats.record_trade(
                strategy    = strategy,
                symbol      = symbol,
                entry_score = entry_score,
                won         = won,
                stake       = stake,
                payout      = payout,
            )
        except Exception as exc:
            logger.warning(f"strategy_stats.record_trade({symbol}) failed: {exc}")

        # Implementation Brief v3, task 3 — same call site as
        # strategy_stats.stats.record_trade() above, so both always agree
        # on trade count.
        try:
            self._strategy_expectancy.record(strategy=strategy, won=won, pnl=pnl)
        except Exception as exc:
            logger.warning(f"strategy_expectancy.record({symbol}) failed: {exc}")

        # Backfill the outcome onto the oldest pending meta-label
        # prediction for this pair, for later validation of the filter.
        try:
            meta_labeling.record_outcome(strategy=strategy, symbol=symbol, won=won)
        except Exception as exc:
            logger.warning(f"meta_labeling.record_outcome({symbol}) failed: {exc}")

        if pnl < 0:
            self._confirmed_daily_loss += abs(pnl)
        self._check_confirmed_loss_limit()

        set_active_trades(len(self._open_contracts))
        update_status(streak=self.risk.current_streak)

        try:
            record_trade(
                symbol        = symbol,
                direction     = info.get("direction", "?"),
                stake         = stake,
                pnl           = pnl,
                balance_after = self.client.balance,
                won           = won,
                strategy      = strategy,
                multiplier    = info.get("multiplier", None),
                close_reason  = close_reason,
            )
        except Exception:
            pass

        try:
            self._push_dashboard()
        except Exception:
            pass

        streak = self.risk.current_streak
        logger.info(
            f"{'✅ WIN' if won else '❌ LOSS'} | "
            f"{symbol} | {strategy} | "
            f"pnl=${pnl:+.4f} | "
            f"balance=${self.client.balance:.4f} | "
            f"streak={streak} | close_reason={close_reason}")

    # ── Contract result callback ───────────────────────────────────────────────

    async def _on_contract_result(self, cid: str, msg: dict):
        poc = msg.get("proposal_open_contract", {})
        if not poc.get("is_sold"):
            return

        info = self._open_contracts.pop(cid, None)
        self._contract_open_times.pop(cid, None)
        self._reconciling.pop(cid, None)
        if not info:
            return

        try:
            self.client.stop_tracking(cid)
        except Exception:
            pass

        await self._apply_settlement(cid, info, poc, close_reason="normal")

    # ── Settle loop — independent of the scan loop ─────────────────────────────

    async def _settle_loop(self):
        settle_wait    = getattr(config, "SETTLE_WAIT_SECS", 15)
        redeploy_every = getattr(config, "REDEPLOY_EVERY_N_CYCLES", 6)
        drain_max_secs = DRAIN_MAX_SECS

        while True:
            try:
                await asyncio.sleep(settle_wait)

                await self._handle_orphans()
                self._check_confirmed_loss_limit()

                self._cycle_count += 1

                # Implementation Brief v2, Fix G: restart_scheduler's daily
                # Kenya-midnight timer is now the single authoritative
                # redeploy trigger (is_redeploy_pending() flips True once
                # per day). REDEPLOY_EVERY_N_CYCLES stays disabled at
                # 999999 by default (see config.py) — kept only so this
                # doesn't silently break if it's ever deliberately lowered.
                redeploy_wanted = (
                    self._cycle_count >= redeploy_every
                    or restart_scheduler.is_redeploy_pending()
                )

                if redeploy_wanted:
                    n_open = len(self._open_contracts)
                    logger.info(
                        f"REDEPLOY TRIGGERED: draining {n_open} open "
                        f"contract(s) before restart")

                    drain_started = time.time()
                    while self._open_contracts:
                        # Actively try to confirm-close everything
                        # remaining — reuses the exact same reconciliation
                        # (Fix C) and Multiplier max-hold (Fix E) machinery
                        # as normal operation. Never a guess.
                        await self._handle_orphans()
                        if not self._open_contracts:
                            break

                        drain_elapsed = time.time() - drain_started
                        if drain_elapsed >= drain_max_secs:
                            stuck = [
                                f"{cid} (age={int(time.time() - info.get('opened_at', time.time()))}s)"
                                for cid, info in self._open_contracts.items()
                            ]
                            logger.warning(
                                f"DRAIN_MAX_SECS ({drain_max_secs}s) exceeded "
                                f"with {len(self._open_contracts)} contract(s) "
                                f"still not confirmed-closed: {', '.join(stuck)} "
                                f"— DELAYING the redeploy rather than wiping "
                                f"their bookkeeping (Requirement 1). Will keep "
                                f"actively trying to close them and re-check "
                                f"on the next settle tick."
                            )
                            break

                        logger.info(
                            f"Draining — {len(self._open_contracts)} "
                            f"contract(s) open, actively confirming closes")
                        await asyncio.sleep(5)

                    if not self._open_contracts:
                        restart_scheduler.trigger_redeploy()
                        logger.info("Redeploy triggered — standing by")
                        self._cycle_count = 0
                    # else: redeploy stays pending. We deliberately do NOT
                    # clear _open_contracts / _contract_open_times here —
                    # the next settle tick will re-enter this branch and
                    # keep trying to drain for real.

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"_settle_loop: {exc}")

    # ── Orphan handling ─────────────────────────────────────────────────────────

    async def _handle_orphans(self):
        now = time.time()
        multiplier_symbols = getattr(config, "MULTIPLIER_SYMBOLS", set())

        for cid, info in list(self._open_contracts.items()):
            opened_at = info.get("opened_at", now)
            age       = now - opened_at
            symbol    = info.get("symbol", "UNKNOWN")

            # Multiplier contracts (no fixed expiry) get their own explicit,
            # active-close-only path (Fix E) — never the Rise/Fall
            # age-based logic below.
            if symbol in multiplier_symbols:
                await self._handle_multiplier_orphan(cid, info, age)
                continue

            # Already in the reconcile-pending state from a previous tick —
            # keep polling on its own cadence rather than re-deriving age.
            if cid in self._reconciling:
                await self._reconcile_pending_contract(cid, info)
                continue

            if age >= CONTRACT_FORCE_CLOSE_SECS:
                await self._begin_reconciliation(cid, info)
            elif age >= CONTRACT_MAX_AGE_SECS:
                try:
                    await self.client.force_check_contract(cid)
                except Exception as exc:
                    logger.warning(f"force_check_contract({cid}): {exc}")

    # ── Reconciliation entry point (Fix C) ──────────────────────────────────
    #
    # Replaces the old "just declare a loss" ORPHAN_TIMEOUT branch. A
    # Rise/Fall contract past CONTRACT_FORCE_CLOSE_SECS gets exactly one
    # more authoritative check; if it's genuinely settled, it's recorded
    # for real through the shared _apply_settlement() helper (same logic
    # _on_contract_result() uses). If not, it moves into reconcile_pending
    # and is retried — it is NEVER marked win/loss on a guess.

    async def _begin_reconciliation(self, cid: str, info: dict) -> None:
        symbol = info.get("symbol", "UNKNOWN")
        try:
            poc = await self.client.force_check_contract(cid)
        except Exception as exc:
            logger.warning(f"force_check_contract({cid}) at reconcile-start: {exc}")
            poc = {}

        if poc.get("is_sold") or poc.get("is_expired"):
            self._open_contracts.pop(cid, None)
            self._contract_open_times.pop(cid, None)
            self._reconciling.pop(cid, None)
            try:
                self.client.stop_tracking(cid)
            except Exception:
                pass
            await self._apply_settlement(cid, info, poc, close_reason="normal")
            return

        now = time.time()
        self._reconciling[cid] = {"reconcile_started_at": now, "last_poll": now}
        logger.warning(
            f"RECONCILE PENDING: {cid} ({symbol}) still open after "
            f"{CONTRACT_FORCE_CLOSE_SECS}s — polling every "
            f"{RECONCILE_POLL_INTERVAL_SECS}s until a real result arrives; "
            f"dashboard keeps it visibly open/pending, never a guessed result"
        )
        try:
            self._push_dashboard()
        except Exception:
            pass

    async def _reconcile_pending_contract(self, cid: str, info: dict) -> None:
        state = self._reconciling.get(cid)
        if not state:
            return

        now = time.time()
        if now - state.get("last_poll", 0) < RECONCILE_POLL_INTERVAL_SECS:
            return
        state["last_poll"] = now

        symbol = info.get("symbol", "UNKNOWN")
        try:
            poc = await self.client.force_check_contract(cid)
        except Exception as exc:
            logger.warning(f"force_check_contract({cid}) during reconcile: {exc}")
            poc = {}

        elapsed = now - state["reconcile_started_at"]

        if poc.get("is_sold") or poc.get("is_expired"):
            self._open_contracts.pop(cid, None)
            self._contract_open_times.pop(cid, None)
            self._reconciling.pop(cid, None)
            try:
                self.client.stop_tracking(cid)
            except Exception:
                pass
            await self._apply_settlement(cid, info, poc, close_reason="reconcile_delayed")
            return

        if elapsed >= RECONCILE_MAX_SECS:
            # Last-resort truth source (Fix C.3) — a different real query,
            # never a guess. If it doesn't find anything either, keep
            # retrying in the background; the contract stays visibly
            # open/pending forever if it has to, but never gets a
            # fabricated win/loss.
            try:
                pt = await self.client.profit_table_lookup(cid)
            except Exception as exc:
                logger.error(f"profit_table_lookup({cid}) failed: {exc}")
                pt = {}

            if pt.get("is_sold"):
                self._open_contracts.pop(cid, None)
                self._contract_open_times.pop(cid, None)
                self._reconciling.pop(cid, None)
                try:
                    self.client.stop_tracking(cid)
                except Exception:
                    pass
                await self._apply_settlement(cid, info, pt, close_reason="reconcile_delayed")
                return

            logger.error(
                f"RECONCILE STILL PENDING: {cid} ({symbol}) unresolved "
                f"after {elapsed:.0f}s (ceiling {RECONCILE_MAX_SECS}s) — "
                f"escalating loudly; still NOT recording a guessed result, "
                f"will keep retrying in the background"
            )

    # ── Multiplier contracts — explicit, active closing only (Fix E) ───────

    async def _handle_multiplier_orphan(self, cid: str, info: dict, age: float) -> None:
        if age < MULTIPLIER_MAX_HOLD_SECS:
            return

        symbol = info.get("symbol", "UNKNOWN")
        logger.info(
            f"MULTIPLIER MAX-HOLD: {cid} ({symbol}) held {age:.0f}s >= "
            f"{MULTIPLIER_MAX_HOLD_SECS}s — actively selling to realize the "
            f"real current price (never a guess, per Fix E / Requirement 1)"
        )
        try:
            sell_resp = await self.client.sell_contract(cid, price=0)
        except Exception as exc:
            logger.error(f"sell_contract({cid}) failed during max-hold close: {exc}")
            sell_resp = None

        if not sell_resp:
            # No confirmed close — this contract must NEVER be removed from
            # _open_contracts without one. Leave it open and tracked; retry
            # next _handle_orphans tick.
            logger.warning(
                f"MULTIPLIER SELL FAILED: {cid} ({symbol}) — still open, "
                f"will retry active close next cycle (no bookkeeping wiped)"
            )
            return

        # One authoritative follow-up check to get profit/sell_price in the
        # same shape _apply_settlement() expects.
        try:
            poc = await self.client.force_check_contract(cid)
        except Exception:
            poc = {}

        if not (poc.get("is_sold") or poc.get("is_expired")):
            # Fall back to the sell response itself — sold_for is a real,
            # confirmed value even if the follow-up check hasn't caught up.
            sold_for = float(sell_resp.get("sold_for", 0))
            stake    = float(info.get("stake", 0.0))
            poc = {
                "is_sold":    1,
                "sell_price": sold_for,
                "profit":     sold_for - stake,
                "payout":     sold_for,
            }

        self._open_contracts.pop(cid, None)
        self._contract_open_times.pop(cid, None)
        self._reconciling.pop(cid, None)
        try:
            self.client.stop_tracking(cid)
        except Exception:
            pass
        await self._apply_settlement(cid, info, poc, close_reason="time_based_close")

    # ── Adaptive Exit Engine — per-contract monitoring loop ─────────────────
    #
    # Runs at config.EXIT_POLL_INTERVAL_SECS (default 15s) — tighter than the
    # general _handle_orphans sweep (30s) — because exit timing matters more
    # for open-ended Multiplier risk than the general health-check sweep
    # does. This is purely additive: _handle_multiplier_orphan's 30-minute
    # MULTIPLIER_MAX_HOLD_SECS force-close remains the untouched outer safety
    # bound and still fires if this task ever stops running (e.g. a restart
    # wipes the task but not yet the open-contract record).
    #
    # Never calls _apply_settlement for a natural close (is_sold/is_expired
    # discovered on a poll) — that's owned by _on_contract_result (WS push)
    # or the orphan sweep. Only calls it for a close *this method* actively
    # triggers via sell_contract (CLOSE_NOW), reusing the same shared
    # settlement path everything else uses.

    async def _monitor_exit(self, cid: str) -> None:
        poll_interval = getattr(config, "EXIT_POLL_INTERVAL_SECS", 15)

        while cid in self._open_contracts:
            try:
                await asyncio.sleep(poll_interval)

                # Re-check after the sleep — the contract may have settled
                # via the WS push path or the orphan sweep while we waited.
                info = self._open_contracts.get(cid)
                if info is None:
                    return

                try:
                    poc = await self.client.force_check_contract(cid)
                except Exception as exc:
                    logger.warning(f"_monitor_exit force_check_contract({cid}): {exc}")
                    continue

                if poc.get("is_sold") or poc.get("is_expired"):
                    # Natural close — the existing settlement path handles
                    # recording it. Not our job; just stop monitoring.
                    return

                symbol      = info.get("symbol", "UNKNOWN")
                opened_at   = info.get("opened_at", time.time())
                elapsed     = time.time() - opened_at
                live_profit = float(poc.get("profit", 0.0))

                try:
                    exit_engine.record_snapshot(
                        cid,
                        symbol  = symbol,
                        profit  = live_profit,
                        elapsed = elapsed,
                        poc     = poc,
                    )
                    decision = exit_engine.decide_exit(
                        cid,
                        symbol  = symbol,
                        profit  = live_profit,
                        elapsed = elapsed,
                        poc     = poc,
                    )
                except Exception as exc:
                    logger.warning(f"exit_engine decision failed for {cid}: {exc}")
                    continue

                action = getattr(decision, "action", None)

                if action == "TRAIL_UPDATE":
                    try:
                        await self.client.contract_update(
                            cid, stop_loss=decision.new_stop_loss)
                        logger.info(
                            f"ADAPTIVE EXIT TRAIL: {cid} ({symbol}) "
                            f"stop_loss -> {decision.new_stop_loss}")
                    except Exception as exc:
                        logger.warning(f"contract_update({cid}) trail failed: {exc}")
                    continue

                if action == "CLOSE_NOW":
                    logger.info(
                        f"ADAPTIVE EXIT CLOSE: {cid} ({symbol}) elapsed="
                        f"{elapsed:.0f}s profit={live_profit:.4f} — actively "
                        f"selling to realize the real current price")
                    try:
                        sell_resp = await self.client.sell_contract(cid, price=0)
                    except Exception as exc:
                        logger.error(f"sell_contract({cid}) failed during adaptive exit: {exc}")
                        sell_resp = None

                    if not sell_resp:
                        # No confirmed close — never drop bookkeeping without
                        # one (same invariant _handle_multiplier_orphan
                        # follows). Leave it open/tracked; retry next tick.
                        logger.warning(
                            f"ADAPTIVE EXIT SELL FAILED: {cid} ({symbol}) — "
                            f"still open, will retry next tick")
                        continue

                    # One authoritative follow-up check, same pattern as
                    # _handle_multiplier_orphan's max-hold close.
                    try:
                        close_poc = await self.client.force_check_contract(cid)
                    except Exception:
                        close_poc = {}

                    if not (close_poc.get("is_sold") or close_poc.get("is_expired")):
                        sold_for = float(sell_resp.get("sold_for", 0))
                        stake    = float(info.get("stake", 0.0))
                        close_poc = {
                            "is_sold":    1,
                            "sell_price": sold_for,
                            "profit":     sold_for - stake,
                            "payout":     sold_for,
                        }

                    final_profit = float(close_poc.get("profit", 0.0))

                    self._open_contracts.pop(cid, None)
                    self._contract_open_times.pop(cid, None)
                    self._reconciling.pop(cid, None)
                    try:
                        self.client.stop_tracking(cid)
                    except Exception:
                        pass

                    await self._apply_settlement(
                        cid, info, close_poc, close_reason="adaptive_exit")

                    try:
                        exit_engine.record_closed(cid, final_profit)
                    except Exception as exc:
                        logger.warning(f"exit_engine.record_closed({cid}) failed: {exc}")

                    return

                # action == "HOLD" (or anything unrecognized) — do nothing,
                # keep looping.

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"_monitor_exit({cid}) tick failed: {exc}")
                continue
