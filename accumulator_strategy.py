"""
accumulator_strategy.py — Accumulator (ACCU) growth-rate selection,
eligibility scoring, historical survival tracking, and systematic exit.

Uses config.ACCU_GROWTH_RATE_MIN / ACCU_GROWTH_RATE_MAX / ACCU_EXIT_FRACTION
and deriv_client.DerivClient's existing get_accumulator_proposal(),
buy_accumulator(), sell_contract(), force_check_contract(), and
subscribe_contract() — none of those are redefined here.

DESIGN NOTES / ASSUMPTIONS (verify against your account's live responses):

1. BARRIER WIDTH SOURCE — growth rate → barrier width is not a documented
   fixed formula; it depends on Deriv's live pricing for each symbol. This
   module gets the real number by calling get_accumulator_proposal() for a
   spread of candidate growth rates and reading the barrier width Deriv
   actually returns, rather than assuming a formula.

   The primary field read is `tick_size_barrier` (fraction of spot, e.g.
   0.001 == 0.1%), which is the field Deriv's ACCU proposal response is
   documented to include. If your account's proposal payload uses a
   different shape, `probe_barrier_width()` falls back to deriving width
   from `high_barrier`/`low_barrier`/`spot`, and if neither is present it
   logs the actual keys returned so you can adjust the extraction — it
   does NOT silently guess a barrier width.

2. VOLATILITY MATCH — realized volatility is the population stdev of
   tick-to-tick % change over recent tick history (TICK_HISTORY_COUNT
   ticks). The target barrier width is
   `realized_vol_pct/100 * BARRIER_VOL_MULTIPLE` (BARRIER_VOL_MULTIPLE=1.0
   by default, i.e. match the barrier to ~1 stdev of tick moves). The
   growth rate whose actual (proposal-derived) barrier width is closest to
   that target is selected — this directly implements "implied barrier
   width best matches realized volatility" without hardcoding whether
   higher growth rate means a wider or narrower barrier; the real
   proposal data decides that per symbol.

3. TICKS-SURVIVED SIMULATION — walks the same tick history used for the
   volatility calc, using the selected growth rate's barrier width as a
   +/- band around each of several sampled entry points, and counts how
   many consecutive ticks stayed inside the band before breaching it.
   This is a historical average, not a guarantee, and is only as good as
   TICK_HISTORY_COUNT ticks of recent history.

4. TICK INTERVAL — used only to convert "ticks" into a wall-clock exit
   window for the profit-hold exit rule. R_* symbols tick ~2s, 1HZ*
   symbols tick ~1s (Deriv's documented nominal intervals). This is an
   approximation for scheduling the exit check, not used for barrier math.

5. PROBE STAKE — get_accumulator_proposal() calls made while scoring
   growth rates use PROBE_STAKE purely to obtain a priced proposal; no
   buy is placed during analysis.
"""

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import config
from deriv_client import DerivClient, PROPOSAL_DELAY

logger = logging.getLogger(__name__)

# ── Symbols ─────────────────────────────────────────────────────────────────
TARGET_SYMBOLS = list(config.VOLATILITY_STANDARD)          # R_10..R_100
TARGET_SYMBOLS_1S_CANDIDATES = [                            # confirmed via
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",      # confirm_1s_accumulator_support()
]

# ── Tunables ────────────────────────────────────────────────────────────────
TICK_HISTORY_COUNT       = 300   # ticks fetched for vol calc + survival sim
GROWTH_RATE_STEP          = 0.5  # candidate spacing within config's min/max
BARRIER_VOL_MULTIPLE      = 1.0  # target barrier width = this * realized stdev
SURVIVAL_SAMPLE_TRIALS    = 20   # sampled entry points for ticks-survived sim
PROBE_STAKE                = 1.0  # nominal stake for proposal-only probing
MONITOR_POLL_INTERVAL_SECS = 5.0  # exit-rule check cadence

_TICK_INTERVAL_SECS = {
    "1HZ": 1.0,
    "R_":  2.0,
}
_DEFAULT_TICK_INTERVAL_SECS = 2.0


def _tick_interval_secs(symbol: str) -> float:
    for prefix, secs in _TICK_INTERVAL_SECS.items():
        if symbol.upper().startswith(prefix):
            return secs
    return _DEFAULT_TICK_INTERVAL_SECS


def _growth_rate_candidates() -> List[float]:
    lo, hi = config.ACCU_GROWTH_RATE_MIN, config.ACCU_GROWTH_RATE_MAX
    if hi <= lo:
        return [lo]
    n = int(round((hi - lo) / GROWTH_RATE_STEP))
    return [round(lo + i * GROWTH_RATE_STEP, 2) for i in range(n + 1)]


def _linspace_indices(start: int, stop: int, num: int) -> List[int]:
    if stop <= start:
        return [start]
    num = max(1, min(num, stop - start + 1))
    if num == 1:
        return [start]
    step = (stop - start) / (num - 1)
    return sorted({int(round(start + i * step)) for i in range(num)})


# ── Data shapes ───────────────────────────────────────────────────────────

@dataclass
class GrowthRateEligibility:
    symbol:                  str
    recommended_growth_rate: float
    eligibility_score:       float             # 0-1, higher = better barrier/volatility match
    barrier_width_pct:       Optional[float]    # % of spot, from live proposal data
    realized_vol_pct:        float              # stdev of tick-to-tick % change
    avg_ticks_survived:      Optional[float]    # historical avg ticks in-range at that barrier


# ── Tick history + volatility ─────────────────────────────────────────────

async def fetch_tick_history(
    client: DerivClient,
    symbol: str,
    count: int = TICK_HISTORY_COUNT,
) -> List[float]:
    """
    Fetch recent raw tick prices for *symbol*.

    deriv_client.get_candles() only exposes OHLC candles (style="candles"),
    not raw ticks, so this calls the same low-level client._send() request
    path get_candles() itself uses, with style="ticks" instead. Returns []
    on any failure — never raises.
    """
    await client._ready.wait()
    try:
        resp = await client._send(
            {
                "ticks_history":     symbol,
                "adjust_start_time": 1,
                "count":             count,
                "end":               "latest",
                "style":             "ticks",
            },
            timeout=20,
        )
        prices = resp.get("history", {}).get("prices", [])
        return [float(p) for p in prices]
    except Exception as exc:
        logger.warning(f"TICK HISTORY FETCH FAILED: {symbol} | error={exc}")
        return []


def compute_realized_volatility(prices: List[float]) -> Tuple[float, int]:
    """
    Population stdev of tick-to-tick % change, in percentage points.
    Returns (0.0, n) if there's not enough history to compute anything
    meaningful.
    """
    if len(prices) < 3:
        return 0.0, len(prices)
    pct_changes = []
    for a, b in zip(prices, prices[1:]):
        if a == 0:
            continue
        pct_changes.append((b - a) / a * 100.0)
    if len(pct_changes) < 2:
        return 0.0, len(pct_changes)
    return statistics.pstdev(pct_changes), len(pct_changes)


# ── Barrier width probing (live proposal data) ────────────────────────────

async def probe_barrier_width(
    client: DerivClient,
    symbol: str,
    growth_rate: float,
    stake: float = PROBE_STAKE,
) -> Optional[float]:
    """
    Request an ACCU proposal for *growth_rate* and extract the implied
    barrier width as a fraction of spot (0.001 == 0.1%). Returns None if
    the proposal fails or the response doesn't contain a barrier field
    this function knows how to read (see module docstring point 1) — the
    actual response keys are logged in that case for you to inspect.
    """
    proposal = await client.get_accumulator_proposal(symbol, stake, growth_rate)
    if not proposal:
        return None

    if "tick_size_barrier" in proposal:
        try:
            return float(proposal["tick_size_barrier"])
        except (TypeError, ValueError):
            pass

    spot = proposal.get("spot")
    high = proposal.get("high_barrier")
    low  = proposal.get("low_barrier")
    try:
        if spot is not None and high is not None and low is not None:
            spot_f, high_f, low_f = float(spot), float(high), float(low)
            if spot_f != 0:
                return abs(high_f - low_f) / spot_f
    except (TypeError, ValueError):
        pass

    logger.warning(
        f"{symbol}: ACCU proposal (growth_rate={growth_rate}%) missing "
        f"tick_size_barrier/high_barrier/low_barrier — got keys={list(proposal.keys())}"
    )
    return None


async def select_growth_rate(
    client: DerivClient,
    symbol: str,
    realized_vol_pct: float,
    stake: float = PROBE_STAKE,
    vol_multiple: float = BARRIER_VOL_MULTIPLE,
) -> Tuple[float, Optional[float], float]:
    """
    Probe every candidate growth rate in [ACCU_GROWTH_RATE_MIN,
    ACCU_GROWTH_RATE_MAX] and pick the one whose live-proposal barrier
    width is closest to `realized_vol_pct/100 * vol_multiple`.

    Returns (growth_rate, barrier_fraction_or_None, eligibility_score).
    If no candidate returns usable barrier data, falls back to the
    midpoint growth rate with eligibility_score=0.0 (low confidence,
    flagged for the caller to filter out if desired).
    """
    target_fraction = (realized_vol_pct / 100.0) * vol_multiple
    best: Optional[Tuple[float, float, float]] = None

    for gr in _growth_rate_candidates():
        barrier_fraction = await probe_barrier_width(client, symbol, gr, stake)
        await asyncio.sleep(PROPOSAL_DELAY)
        if barrier_fraction is None:
            continue
        distance = abs(barrier_fraction - target_fraction)
        score = max(0.0, 1.0 - distance / max(target_fraction, 1e-9))
        if best is None or score > best[2]:
            best = (gr, barrier_fraction, score)

    if best is None:
        mid = round((config.ACCU_GROWTH_RATE_MIN + config.ACCU_GROWTH_RATE_MAX) / 2, 2)
        logger.warning(
            f"{symbol}: no usable barrier data from any ACCU proposal — "
            f"defaulting growth_rate={mid}% with eligibility_score=0.0"
        )
        return mid, None, 0.0

    return best


# ── Historical ticks-survived simulation ───────────────────────────────────

def simulate_ticks_survived(
    prices: List[float],
    barrier_fraction: float,
    trials: int = SURVIVAL_SAMPLE_TRIALS,
) -> Optional[float]:
    """
    Historical average of how many consecutive ticks price stayed within
    +/- barrier_fraction of an entry price, sampled at `trials` evenly
    spaced starting points across `prices`.
    """
    n = len(prices)
    if n < 10 or barrier_fraction is None or barrier_fraction <= 0:
        return None

    max_start = n - 2
    if max_start <= 0:
        return None

    survived_counts = []
    for start in _linspace_indices(0, max_start, trials):
        entry_price = prices[start]
        if entry_price == 0:
            continue
        upper = entry_price * (1 + barrier_fraction)
        lower = entry_price * (1 - barrier_fraction)
        count = 0
        for p in prices[start + 1:]:
            if p > upper or p < lower:
                break
            count += 1
        survived_counts.append(count)

    if not survived_counts:
        return None
    return statistics.fmean(survived_counts)


# ── Per-symbol / batch analysis ────────────────────────────────────────────

async def analyze_symbol(client: DerivClient, symbol: str) -> GrowthRateEligibility:
    """
    Full pipeline for one symbol: realized volatility -> growth rate
    selection (via live barrier probing) -> historical ticks-survived.
    """
    prices = await fetch_tick_history(client, symbol)
    if len(prices) < 10:
        logger.warning(f"{symbol}: insufficient tick history ({len(prices)} ticks) — skipping")
        return GrowthRateEligibility(
            symbol=symbol,
            recommended_growth_rate=config.ACCU_GROWTH_RATE_MIN,
            eligibility_score=0.0,
            barrier_width_pct=None,
            realized_vol_pct=0.0,
            avg_ticks_survived=None,
        )

    realized_vol_pct, _ = compute_realized_volatility(prices)
    growth_rate, barrier_fraction, score = await select_growth_rate(client, symbol, realized_vol_pct)

    avg_ticks_survived = None
    if barrier_fraction is not None:
        avg_ticks_survived = simulate_ticks_survived(prices, barrier_fraction)

    result = GrowthRateEligibility(
        symbol=symbol,
        recommended_growth_rate=growth_rate,
        eligibility_score=round(score, 4),
        barrier_width_pct=round(barrier_fraction * 100, 4) if barrier_fraction is not None else None,
        realized_vol_pct=round(realized_vol_pct, 4),
        avg_ticks_survived=round(avg_ticks_survived, 2) if avg_ticks_survived is not None else None,
    )
    logger.info(
        f"ACCU ANALYSIS: {symbol} | growth_rate={result.recommended_growth_rate}% | "
        f"score={result.eligibility_score} | barrier={result.barrier_width_pct}% | "
        f"realized_vol={result.realized_vol_pct}% | avg_ticks_survived={result.avg_ticks_survived}"
    )
    return result


async def analyze_all_symbols(
    client: DerivClient,
    symbols: Optional[List[str]] = None,
) -> Dict[str, GrowthRateEligibility]:
    symbols = symbols or TARGET_SYMBOLS
    results: Dict[str, GrowthRateEligibility] = {}
    for symbol in symbols:
        results[symbol] = await analyze_symbol(client, symbol)
    return results


async def confirm_1s_accumulator_support(
    client: DerivClient,
    candidates: Optional[List[str]] = None,
    stake: float = PROBE_STAKE,
) -> List[str]:
    """
    Live-probes each 1s volatility symbol with a minimal ACCU proposal
    request (growth_rate=ACCU_GROWTH_RATE_MIN) and returns the subset that
    returns a valid proposal, i.e. genuinely supports Accumulators on this
    account — rather than trusting a static symbol list.
    """
    candidates = candidates or TARGET_SYMBOLS_1S_CANDIDATES
    supported = []
    for symbol in candidates:
        proposal = await client.get_accumulator_proposal(symbol, stake, config.ACCU_GROWTH_RATE_MIN)
        await asyncio.sleep(PROPOSAL_DELAY)
        if proposal:
            supported.append(symbol)
            logger.info(f"{symbol}: ACCU supported (1s variant)")
        else:
            logger.info(f"{symbol}: ACCU NOT supported (1s variant) — proposal rejected")
    return supported


# ── Systematic exit rule ────────────────────────────────────────────────────

class AccumulatorPositionMonitor:
    """
    Tracks open ACCU positions and closes each one, via
    deriv_client.sell_contract(), once its profit has held continuously
    for config.ACCU_EXIT_FRACTION of the symbol's historical average
    ticks-survived (converted to a wall-clock window using the symbol's
    nominal tick interval) — instead of holding to knockout or a fixed
    profit target.
    """

    def __init__(self, client: DerivClient, poll_interval: float = MONITOR_POLL_INTERVAL_SECS):
        self.client = client
        self.poll_interval = poll_interval
        self._positions: Dict[str, dict] = {}   # contract_id -> tracking state

    def track(
        self,
        contract_id: str,
        symbol: str,
        avg_ticks_survived: Optional[float],
        exit_fraction: Optional[float] = None,
    ) -> None:
        exit_fraction = config.ACCU_EXIT_FRACTION if exit_fraction is None else exit_fraction
        exit_after_secs = None
        if avg_ticks_survived:
            exit_after_secs = avg_ticks_survived * _tick_interval_secs(symbol) * exit_fraction

        cid = str(contract_id)
        self._positions[cid] = {
            "symbol":          symbol,
            "exit_after_secs": exit_after_secs,
            "profit_since":    None,   # monotonic time profit last turned positive
        }
        logger.info(
            f"TRACKING FOR EXIT: {cid} ({symbol}) | "
            f"exit after profit held {exit_after_secs}s "
            f"(avg_ticks_survived={avg_ticks_survived}, fraction={exit_fraction})"
        )

    def untrack(self, contract_id: str) -> None:
        self._positions.pop(str(contract_id), None)

    async def _check_once(self, contract_id: str) -> None:
        info = self._positions.get(contract_id)
        if info is None:
            return

        poc = await self.client.force_check_contract(contract_id)
        if not poc:
            return

        if poc.get("is_sold") or poc.get("is_expired"):
            logger.info(f"ALREADY CLOSED: {contract_id} ({info['symbol']}) — dropping from monitor")
            self._positions.pop(contract_id, None)
            return

        profit = float(poc.get("profit", 0) or 0)
        now = time.monotonic()

        if profit <= 0:
            info["profit_since"] = None
            return

        if info["profit_since"] is None:
            info["profit_since"] = now

        held_secs = now - info["profit_since"]
        exit_after_secs = info["exit_after_secs"]

        if exit_after_secs is not None and held_secs >= exit_after_secs:
            sell_price = float(poc.get("bid_price", 0) or 0)
            logger.info(
                f"SYSTEMATIC EXIT TRIGGERED: {contract_id} ({info['symbol']}) | "
                f"profit=${profit:.4f} held {held_secs:.1f}s >= target {exit_after_secs:.1f}s"
            )
            result = await self.client.sell_contract(contract_id, sell_price)
            if result:
                logger.info(
                    f"SYSTEMATIC EXIT DONE: {contract_id} sold_for=${result.get('sold_for')}"
                )
            else:
                logger.warning(f"SYSTEMATIC EXIT FAILED: {contract_id} — sell_contract returned None")
            self._positions.pop(contract_id, None)

    async def run(self) -> None:
        """Long-running monitor loop — schedule with asyncio.create_task()."""
        while True:
            await asyncio.sleep(self.poll_interval)
            if not self._positions:
                continue
            for cid in list(self._positions.keys()):
                try:
                    await self._check_once(cid)
                except Exception as exc:
                    logger.error(f"MONITOR ERROR {cid}: {exc}")


# ── Orchestrator ─────────────────────────────────────────────────────────

class AccumulatorStrategy:
    """
    Ties analysis (growth rate selection + eligibility) to execution
    (buy_accumulator) and the systematic exit rule (AccumulatorPositionMonitor).
    """

    def __init__(self, client: DerivClient, poll_interval: float = MONITOR_POLL_INTERVAL_SECS):
        self.client = client
        self.monitor = AccumulatorPositionMonitor(client, poll_interval=poll_interval)
        self.eligibility: Dict[str, GrowthRateEligibility] = {}
        self._monitor_task: Optional[asyncio.Task] = None

    async def refresh_eligibility(self, symbols: Optional[List[str]] = None) -> Dict[str, GrowthRateEligibility]:
        self.eligibility = await analyze_all_symbols(self.client, symbols or TARGET_SYMBOLS)
        return self.eligibility

    def start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self.monitor.run())

    async def enter_position(
        self,
        symbol: str,
        stake: float,
        take_profit: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Buys an ACCU contract on *symbol* using the growth rate from the
        most recent refresh_eligibility() call, then registers it with the
        exit monitor and the client's own polling/subscription machinery.
        Returns None on any failure — never raises.
        """
        elig = self.eligibility.get(symbol)
        if elig is None:
            logger.warning(f"{symbol}: no eligibility data — call refresh_eligibility() first")
            return None

        result = await self.client.buy_accumulator(
            symbol, stake, elig.recommended_growth_rate, take_profit=take_profit
        )
        if not result:
            return None

        contract_id = str(result.get("contract_id", ""))
        if contract_id:
            self.monitor.track(contract_id, symbol, elig.avg_ticks_survived)
            await self.client.subscribe_contract(contract_id, self._on_contract_closed, symbol=symbol)
            self.start_monitor()
        return result

    async def _on_contract_closed(self, poc_msg: dict) -> None:
        poc = poc_msg.get("proposal_open_contract", {})
        cid = str(poc.get("contract_id", ""))
        self.monitor.untrack(cid)
        logger.info(f"CONTRACT CLOSED (callback): {cid} | profit={poc.get('profit')}")


# ── Standalone analysis run (no trading) ───────────────────────────────────

async def _demo() -> None:
    """
    Connects, runs eligibility analysis across TARGET_SYMBOLS plus a live
    check of the 1s variants, and prints results. Does not place any
    trades. Run with: python accumulator_strategy.py
    """
    logging.basicConfig(level=logging.INFO)
    client = DerivClient()
    await client.connect()

    results = await analyze_all_symbols(client, TARGET_SYMBOLS)
    for symbol, elig in results.items():
        print(
            f"{symbol}: growth_rate={elig.recommended_growth_rate}% "
            f"score={elig.eligibility_score} barrier={elig.barrier_width_pct}% "
            f"realized_vol={elig.realized_vol_pct}% "
            f"avg_ticks_survived={elig.avg_ticks_survived}"
        )

    supported_1s = await confirm_1s_accumulator_support(client)
    print(f"1s variants supporting ACCU: {supported_1s}")


if __name__ == "__main__":
    asyncio.run(_demo())
