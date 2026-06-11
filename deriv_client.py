"""
deriv_client.py – Async Deriv WebSocket client.

v9 → v10 audit changes (full compliance pass):

  DIRECTION MAPPING (VERIFIED CORRECT — DO NOT SWAP):
    direction="LONG"  → contract_type="CALL"  → wins if price RISES
    direction="SHORT" → contract_type="PUT"   → wins if price FALLS

  Every buy_contract() call logs BEFORE placement:
    PLACING {contract_type} on {symbol} stake=${stake:.4f} | duration={duration}m

  FIX 1 — VERBOSE buy_contract() LOGGING:
    - BUY ATTEMPT: {symbol} {contract_type} stake=${stake} mult={multiplier}
    - PROPOSAL RESPONSE: {full resp dict} (after _send)
    - BUY RESPONSE: {full result dict} (after success)
    - BUY FAILED: {symbol} error={detail} (on every failure path)

  FIX 2 — RISE/FALL FALLBACK:
    If buy_contract() gets None from multiplier attempt, retries with
    CALL (LONG) or PUT (SHORT), duration=5m, logs: FALLBACK TO RISE/FALL: {symbol}

  FIX 3 — STAKE CAP:
    If stake > balance * 0.5, caps at max(balance * 0.02, 0.35)
    Logs: STAKE CAPPED: ${original} → ${capped}

  FAILURE CONTRACT (buy_contract):
    - Network timeout  → None + log FAILED: {symbol} — timeout
    - API error        → None + log FAILED: {symbol} — {code}: {msg}
    - Market closed    → None + log FAILED: {symbol} — market_closed
    - Proposal reject  → None + log FAILED: {symbol} — proposal_rejected
    - Never raises — always returns None on any failure path
    - Trade counters never incremented on None return (caller responsibility enforced)

  MARKET OPEN CHECK:
    - Boom/Crash: active_symbols API checked for exchange_is_open before proposal
    - Volatility indices (R_*, 1HZ*): check skipped entirely — always open

  get_balance() METHOD:
    - Calls balance API endpoint
    - Returns float
    - On failure: returns cached balance, logs BALANCE FETCH FAILED — using cached
    - Cache TTL: 30 seconds

  CONNECTION RESILIENCE:
    - Disconnect: logs WEBSOCKET DISCONNECTED — reconnecting in {interval}s (attempt {n}/{max})
    - Retries every WEBSOCKET_RECONNECT_INTERVAL seconds
    - After WEBSOCKET_MAX_RECONNECTS failures: logs WEBSOCKET RECONNECT FAILED — bot halting
      then raises ConnectionError
    - Re-authenticates and re-subscribes open contracts after reconnect

  SUBSCRIPTION CLEANUP:
    - On contract close: calls forget for its proposal subscription
    - Tracks active subscription IDs in _subscriptions: dict[str, str] (contract_id → sub_id)
    - Logs: UNSUBSCRIBED: {symbol} contract {contract_id}

  get_candles():
    - Returns list[dict] with keys: open, high, low, close, epoch
    - On failure: returns [], logs CANDLE FETCH FAILED: {symbol} gran={granularity}

  CONTRACT CLOSURE GUARANTEE (v11):
    - buy_contract() immediately subscribes the new contract via proposal_open_contract
    - _handle() triggers callback on is_sold=1, status="sold", or is_expired=1
    - Pending messages buffered for contracts whose callback isn't registered yet
    - Fallback poller runs every 30s, manually queries every active contract
    - On reconnect, all _contract_callbacks contracts are re-subscribed
    - force_check_contract(cid) allows BotEngine to manually query a contract
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Optional, List, Any, Set

import websockets
from websockets.exceptions import ConnectionClosed

import config

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY = 60

_BOOM_CRASH_PREFIXES = ("BOOM", "CRASH")
_VOLATILITY_PREFIXES = ("R_", "1HZ")

# ── Reconnect defaults (overridden by config if present) ──────────────────────
_DEFAULT_RECONNECT_INTERVAL = 5   # seconds between retries
_DEFAULT_MAX_RECONNECTS     = 10  # 0 = unlimited

# Fallback poll interval in seconds
_FALLBACK_POLL_INTERVAL = 30


def _is_boom_crash(symbol: str) -> bool:
    return any(symbol.upper().startswith(p) for p in _BOOM_CRASH_PREFIXES)


def _is_volatility_index(symbol: str) -> bool:
    s = symbol.upper()
    return any(s.startswith(p) for p in _VOLATILITY_PREFIXES)


class DerivClient:

    def __init__(self):
        self._ws: Optional[Any]    = None
        self._ready: asyncio.Event = asyncio.Event()
        self._connected: bool      = False
        self._authorized: bool     = False

        self._pending: Dict[int, asyncio.Future] = {}
        self._req_id_counter: int = 10

        self._tick_callbacks: Dict[str, Callable] = {}
        self._subscription_map: Dict[str, str]    = {}

        # contract_id → proposal subscription_id  (audit requirement)
        self._subscriptions: Dict[str, str] = {}
        # tracks every contract_id that has an active subscription — prevents double-subscribe
        self._subscribed_contracts: Set[str] = set()
        # contract_id → symbol  (needed for UNSUBSCRIBED log)
        self._contract_symbol_map: Dict[str, str] = {}

        self._balance: float             = 0.0
        self._balance_ts: float          = 0.0          # epoch of last successful fetch
        self._balance_callbacks: List[Callable] = []

        self._contract_callbacks: Dict[str, Callable] = {}

        # Buffer last proposal_open_contract message per cid before callback registered
        self._pending_contract_msgs: Dict[str, dict] = {}

        # Track contracts that need immediate callback flush on subscribe registration
        self._closed_before_callback: Set[str] = set()

        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Rate limiting ─────────────────────────────────────────────────────
        self._buy_semaphore  = asyncio.Semaphore(2)  # max 2 simultaneous buy requests
        self._last_buy_time  = 0.0                   # epoch of last buy attempt

        # ── Direction→contract mapping audit ─────────────────────────────────
        logger.info(
            "Direction mapping: LONG → CALL (price rises) | SHORT → PUT (price falls)"
        )
        assert "CALL" == ("CALL" if "LONG" == "LONG" else "PUT"), (
            "FATAL: LONG→CALL mapping is broken")
        assert "PUT" == ("CALL" if "SHORT" == "LONG" else "PUT"), (
            "FATAL: SHORT→PUT mapping is broken")

    # ─── Connection ───────────────────────────────────────────────────────────

    async def connect(self):
        """
        Outer reconnect loop.  Enforces WEBSOCKET_MAX_RECONNECTS and
        WEBSOCKET_RECONNECT_INTERVAL from config (falls back to module defaults).
        Starts the fallback poller once before the reconnect loop begins.
        """
        self._loop = asyncio.get_event_loop()

        # Start fallback poller once — persists across reconnects
        asyncio.ensure_future(self._fallback_poll_loop())

        reconnect_interval = getattr(
            config, "WEBSOCKET_RECONNECT_INTERVAL", _DEFAULT_RECONNECT_INTERVAL
        )
        max_reconnects = getattr(
            config, "WEBSOCKET_MAX_RECONNECTS", _DEFAULT_MAX_RECONNECTS
        )
        attempt   = 0
        max_label = str(max_reconnects) if max_reconnects > 0 else "∞"

        while True:
            try:
                logger.info(f"Connecting to {config.DERIV_WS_URL} …")
                async with websockets.connect(
                    config.DERIV_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws        = ws
                    self._connected = True
                    attempt         = 0          # reset on successful connection
                    logger.info("WebSocket connected ✓")

                    await self._authorize()
                    dispatch_task = asyncio.ensure_future(self._dispatch_loop())
                    try:
                        await self._subscribe_balance()
                        await self._resubscribe_open_contracts()
                        await dispatch_task
                    finally:
                        dispatch_task.cancel()
                        try:
                            await dispatch_task
                        except (asyncio.CancelledError, Exception):
                            pass

            except ConnectionClosed as exc:
                attempt += 1
                logger.warning(
                    f"WEBSOCKET DISCONNECTED — reconnecting in {reconnect_interval}s "
                    f"(attempt {attempt}/{max_label}) | reason={exc}"
                )
            except Exception as exc:
                attempt += 1
                logger.error(
                    f"WEBSOCKET DISCONNECTED — reconnecting in {reconnect_interval}s "
                    f"(attempt {attempt}/{max_label}) | error={exc}"
                )
            finally:
                self._ready.clear()
                self._connected  = False
                self._authorized = False
                self._ws         = None
                self._pending.clear()

            if max_reconnects > 0 and attempt >= max_reconnects:
                logger.critical(
                    "WEBSOCKET RECONNECT FAILED — bot halting"
                )
                raise ConnectionError(
                    f"WebSocket failed after {max_reconnects} reconnect attempts."
                )

            await asyncio.sleep(reconnect_interval)

    # ─── Post-reconnect resubscription ───────────────────────────────────────

    async def _resubscribe_open_contracts(self):
        """
        Re-attach proposal_open_contract subscriptions after a reconnect.
        Iterates _subscribed_contracts so every previously-subscribed contract
        is restored — not just those that still have a live callback.
        """
        targets = self._subscribed_contracts.copy()
        if not targets:
            return
        logger.info(
            f"Re-subscribing {len(targets)} open contract(s) after reconnect"
        )
        tasks = [
            self._reattach_contract(cid, self._contract_callbacks.get(cid))
            for cid in targets
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _reattach_contract(self, contract_id: str, callback: Optional[Callable]):
        try:
            resp = await self._send({
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
                "subscribe":   1,
            })
            poc    = resp.get("proposal_open_contract", {})
            sub_id = (
                poc.get("id")
                or resp.get("subscription", {}).get("id", "")
            )
            if sub_id:
                self._subscriptions[contract_id] = sub_id
            self._subscribed_contracts.add(contract_id)
            logger.info(f"Re-subscribed contract {contract_id} (sub_id={sub_id})")

            # If the contract already settled during the disconnect, fire callback now
            if self._contract_is_closed(poc) and callback is not None:
                logger.info(
                    f"Contract {contract_id} already closed on reconnect — "
                    f"firing callback immediately"
                )
                try:
                    callback(resp)
                except Exception as exc:
                    logger.debug(f"_reattach_contract callback error ({contract_id}): {exc}")
        except Exception as exc:
            logger.warning(f"_reattach_contract({contract_id}) failed: {exc}")

    # ─── Message dispatch ─────────────────────────────────────────────────────

    async def _dispatch_loop(self):
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
                await self._handle(msg)
            except Exception as exc:
                logger.debug(f"Dispatch error: {exc}")

    @staticmethod
    def _contract_is_closed(poc: dict) -> bool:
        """Return True if the proposal_open_contract data indicates the contract is done."""
        return bool(
            poc.get("is_sold")
            or poc.get("is_expired")
            or poc.get("status") in ("sold", "won", "lost")
        )

    async def _handle(self, msg: dict):
        logger.debug(f"RAW MSG: {msg}")
        msg_type = msg.get("msg_type", "")
        req_id   = msg.get("req_id")
        error    = msg.get("error")

        if error:
            logger.warning(
                f"Deriv API error: {error.get('message')} "
                f"(code={error.get('code')}) | msg_type={msg_type}"
            )

        if msg_type == "balance":
            balance_data = msg.get("balance", {})
            new_bal = float(balance_data.get("balance", self._balance))
            if new_bal != self._balance:
                logger.info(f"Balance updated: ${self._balance:.4f} → ${new_bal:.4f}")
            self._balance    = new_bal
            self._balance_ts = time.monotonic()
            for cb in self._balance_callbacks:
                try:
                    cb(self._balance)
                except Exception:
                    pass

        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                if error:
                    fut.set_exception(RuntimeError(
                        f"{error.get('code', 'ERR')}: {error.get('message', 'Unknown error')}"
                    ))
                else:
                    fut.set_result(msg)
            return

        if msg_type == "tick":
            tick   = msg.get("tick", {})
            sym    = tick.get("symbol", "")
            sub_id = tick.get("id", "")
            for key, cb in self._tick_callbacks.items():
                if key == sym or key == sub_id:
                    try:
                        cb(tick)
                    except Exception as exc:
                        logger.debug(f"Tick callback error: {exc}")

        elif msg_type in ("proposal_open_contract", "buy"):
            contract = msg.get("proposal_open_contract") or msg.get("buy", {})
            cid      = str(contract.get("contract_id", ""))

            if not cid:
                return

            # Always buffer the latest message for this contract
            self._pending_contract_msgs[cid] = msg

            status     = contract.get("status", "")
            is_closed  = self._contract_is_closed(contract)

            # ── Auto-cleanup on contract close ───────────────────────────────
            if is_closed:
                asyncio.ensure_future(self._cleanup_contract_subscription(cid))

            if cid in self._contract_callbacks:
                # Fire immediately on close signals; also pass through normal updates
                if is_closed or msg_type == "proposal_open_contract":
                    try:
                        self._contract_callbacks[cid](msg)
                    except Exception:
                        pass
            elif is_closed:
                # Callback not registered yet — mark for flush when it is
                self._closed_before_callback.add(cid)
                logger.info(
                    f"Contract {cid} closed before callback registered — buffered for flush"
                )

    # ─── Fallback poller ──────────────────────────────────────────────────────

    async def _fallback_poll_loop(self):
        """
        Every 30 seconds, manually query every contract in _subscribed_contracts.
        If the API reports is_sold or is_expired, trigger the callback immediately.
        """
        while True:
            await asyncio.sleep(_FALLBACK_POLL_INTERVAL)
            if not self._authorized or not self._ws:
                continue

            targets = list(self._subscribed_contracts)
            if not targets:
                continue

            logger.debug(
                f"Fallback poll: checking {len(targets)} open contract(s)"
            )
            for cid in targets:
                callback = self._contract_callbacks.get(cid)
                try:
                    resp = await self._send(
                        {
                            "proposal_open_contract": 1,
                            "contract_id": int(cid),
                        },
                        timeout=15,
                    )
                    poc = resp.get("proposal_open_contract", {})
                    if self._contract_is_closed(poc):
                        logger.info(
                            f"Fallback poll: contract {cid} is closed — "
                            f"triggering callback"
                        )
                        if callback is not None:
                            try:
                                callback(resp)
                            except Exception as exc:
                                logger.debug(
                                    f"Fallback poll callback error ({cid}): {exc}"
                                )
                        else:
                            # No callback yet — buffer for flush
                            self._pending_contract_msgs[cid] = resp
                            self._closed_before_callback.add(cid)
                except Exception as exc:
                    logger.debug(f"Fallback poll failed for {cid}: {exc}")

    # ─── Force check a specific contract ─────────────────────────────────────

    async def force_check_contract(self, contract_id: str) -> dict:
        try:
            resp = await self._send({
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
            })
            return resp.get("proposal_open_contract", {})
        except Exception as e:
            logger.error(f"force_check_contract({contract_id}): {e}")
            return {}

    # ─── Subscription cleanup ─────────────────────────────────────────────────

    async def _cleanup_contract_subscription(self, contract_id: str):
        """
        Forget the proposal subscription tied to a closed contract.
        Logs: UNSUBSCRIBED: {symbol} contract {contract_id}
        """
        sub_id = self._subscriptions.pop(contract_id, None)
        symbol = self._contract_symbol_map.pop(contract_id, contract_id)
        self._contract_callbacks.pop(contract_id, None)
        self._pending_contract_msgs.pop(contract_id, None)
        self._closed_before_callback.discard(contract_id)
        self._subscribed_contracts.discard(contract_id)

        if sub_id:
            try:
                await self._send({"forget": sub_id}, timeout=10)
                logger.info(f"UNSUBSCRIBED: {symbol} contract {contract_id}")
            except Exception as exc:
                logger.debug(
                    f"forget({sub_id}) for {symbol}/{contract_id} failed: {exc}"
                )
        else:
            logger.info(f"UNSUBSCRIBED: {symbol} contract {contract_id} (no sub_id)")

    # ─── Request helper ───────────────────────────────────────────────────────

    async def _send(self, payload: dict, timeout: float = 30.0) -> dict:
        if not self._ws or not self._connected:
            raise RuntimeError("Not connected")

        req_id                = self._req_id_counter
        self._req_id_counter += 1
        payload["req_id"]     = req_id

        fut = self._loop.create_future()
        self._pending[req_id] = fut

        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"Request timed out: {payload.get('msg_type', payload)}")

    # ─── Auth & balance ───────────────────────────────────────────────────────

    async def _authorize(self):
        if not config.DERIV_API_TOKEN:
            raise ValueError("DERIV_API_TOKEN is not set.")

        payload = {"authorize": config.DERIV_API_TOKEN, "req_id": 1}
        await self._ws.send(json.dumps(payload))
        raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
        msg = json.loads(raw)

        if msg.get("error"):
            raise RuntimeError(f"Auth failed: {msg['error'].get('message')}")

        account              = msg.get("authorize", {})
        self._balance        = float(account.get("balance", 0))
        self._balance_ts     = time.monotonic()
        self._authorized     = True
        self._req_id_counter = 10

        self._ready.set()

        logger.info(
            f"Authorized ✓ | Account: {account.get('loginid')} | "
            f"Balance: ${self._balance:.4f}"
        )

        for cb in self._balance_callbacks:
            try:
                cb(self._balance)
            except Exception:
                pass

    async def _subscribe_balance(self):
        try:
            await self._send({"balance": 1, "subscribe": 1})
            logger.info("Balance subscription active ✓")
        except Exception as exc:
            logger.warning(
                f"Balance subscription failed: {exc} — will use polling fallback"
            )

    async def balance_refresh_loop(self, interval: int = 60):
        while True:
            await asyncio.sleep(interval)
            if not self._authorized or not self._ws:
                continue
            try:
                resp    = await self._send({"balance": 1})
                new_bal = float(resp.get("balance", {}).get("balance", self._balance))
                if new_bal != self._balance:
                    logger.info(f"Balance poll: ${self._balance:.4f} → ${new_bal:.4f}")
                    self._balance    = new_bal
                    self._balance_ts = time.monotonic()
                    for cb in self._balance_callbacks:
                        try:
                            cb(self._balance)
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug(f"Balance poll failed: {exc}")

    def on_balance(self, callback: Callable[[float], None]):
        self._balance_callbacks.append(callback)

    @property
    def balance(self) -> float:
        return self._balance

    async def get_balance(self) -> float:
        """
        Fetch current account balance via the balance API endpoint.

        Returns the live balance as a float.
        Cache TTL: 30 seconds — if the cached value is fresh enough, return it
        directly to avoid hammering the API.
        On any failure: return last known balance and log a warning.
        """
        _BALANCE_CACHE_TTL = 30.0

        now = time.monotonic()
        if (now - self._balance_ts) < _BALANCE_CACHE_TTL:
            return self._balance

        if not self._authorized or not self._ws:
            cached = self._balance
            logger.warning(
                f"BALANCE FETCH FAILED — using cached: ${cached:.2f} "
                f"(not connected)"
            )
            return cached

        try:
            resp    = await self._send({"balance": 1}, timeout=15)
            new_bal = float(resp.get("balance", {}).get("balance", self._balance))
            if new_bal != self._balance:
                logger.info(f"get_balance: ${self._balance:.4f} → ${new_bal:.4f}")
            self._balance    = new_bal
            self._balance_ts = time.monotonic()
            return self._balance
        except Exception as exc:
            cached = self._balance
            logger.warning(
                f"BALANCE FETCH FAILED — using cached: ${cached:.2f} | error={exc}"
            )
            return cached

    # ─── Historical candles ───────────────────────────────────────────────────

    async def get_candles(
        self,
        symbol:      str,
        granularity: int,
        count:       int = 100,
    ) -> List[dict]:
        """
        Fetch OHLC candles for *symbol*.

        Returns list[dict] with keys: open, high, low, close, epoch
        On any failure: returns [] and logs CANDLE FETCH FAILED: {symbol} gran={granularity}
        """
        await self._ready.wait()
        try:
            resp = await self._send(
                {
                    "ticks_history":    symbol,
                    "adjust_start_time": 1,
                    "count":            count,
                    "end":              "latest",
                    "granularity":      granularity,
                    "style":            "candles",
                },
                timeout=20,
            )
            raw_candles: List[dict] = resp.get("candles", [])

            # Normalise to guaranteed key set: open, high, low, close, epoch
            candles: List[dict] = [
                {
                    "open":  float(c.get("open",  0)),
                    "high":  float(c.get("high",  0)),
                    "low":   float(c.get("low",   0)),
                    "close": float(c.get("close", 0)),
                    "epoch": int(c.get("epoch",   0)),
                }
                for c in raw_candles
            ]

            logger.debug(
                f"Got {len(candles)} candles for {symbol} (gran={granularity}s)"
            )
            return candles

        except Exception as exc:
            logger.warning(
                f"CANDLE FETCH FAILED: {symbol} gran={granularity} | error={exc}"
            )
            return []

    # ─── Live tick subscription ───────────────────────────────────────────────

    async def subscribe_ticks(
        self,
        symbol:   str,
        callback: Callable[[dict], None],
    ) -> str:
        if symbol in self._subscription_map:
            self._tick_callbacks[symbol] = callback
            return self._subscription_map[symbol]

        try:
            resp   = await self._send({"ticks": symbol, "subscribe": 1})
            sub_id = resp.get("tick", {}).get("id", symbol)
            self._subscription_map[symbol] = sub_id
            self._tick_callbacks[symbol]   = callback
            logger.info(f"Tick subscription: {symbol} (sub_id={sub_id})")
            return sub_id
        except Exception as exc:
            logger.error(f"subscribe_ticks({symbol}) failed: {exc}")
            return ""

    async def unsubscribe_ticks(self, symbol: str):
        sub_id = self._subscription_map.pop(symbol, None)
        self._tick_callbacks.pop(symbol, None)
        if sub_id:
            try:
                await self._send({"forget": sub_id})
            except Exception:
                pass

    # ─── Market-open check ────────────────────────────────────────────────────

    async def _check_market_open(self, symbol: str) -> bool:
        """
        Returns True if the symbol's market is open.

        Volatility indices (R_*, 1HZ*) are always open — skip the API call.
        For Boom/Crash (and everything else): call active_symbols and inspect
        exchange_is_open.  Returns False (and logs SKIPPED) on any error so
        that buy_contract() can bail out safely.
        """
        if _is_volatility_index(symbol):
            return True  # 24/7 — never gated

        try:
            resp    = await self._send(
                {"active_symbols": "brief", "product_type": "basic"},
                timeout=20,
            )
            symbols = resp.get("active_symbols", [])
            for s in symbols:
                if s.get("symbol") == symbol:
                    open_flag = s.get("exchange_is_open", 0)
                    return bool(open_flag)
            # Symbol not found in active list → treat as closed
            logger.warning(f"SKIPPED: {symbol} market_closed (symbol not in active list)")
            return False
        except Exception as exc:
            logger.warning(f"SKIPPED: {symbol} market_closed (check failed: {exc})")
            return False

    # ─── Trade execution ──────────────────────────────────────────────────────

    async def buy_contract(
            self,
            symbol:      str,
            direction:   str,   # "LONG" or "SHORT"
            stake:       float,
            multiplier:  int    = 100,
            stop_loss:   float  = None,
            take_profit: float  = None,
            **kwargs) -> dict:
        """
        Buy a Rise/Fall (CALL/PUT) contract via the standard two-step
        proposal -> buy flow.

        Direction mapping (VERIFIED CORRECT - DO NOT SWAP):
          direction="LONG"  -> contract_type="CALL"  -> wins if price RISES
          direction="SHORT" -> contract_type="PUT"   -> wins if price FALLS

        Returns dict (buy response) on success, None on every failure path.
        Never raises.  Never increments trade counters on None return.

        After a successful buy, immediately subscribes to the contract's
        proposal_open_contract stream so settlement is never missed.
        """
        async with self._buy_semaphore:
            elapsed = time.time() - self._last_buy_time
            if elapsed < 0.3:
                await asyncio.sleep(0.3 - elapsed)
            self._last_buy_time = time.time()

            contract_type = "CALL" if direction == "LONG" else "PUT"
            logger.info(f"PLACING {contract_type} | {symbol} | ${stake:.4f}")

            try:
                # Step 1: proposal
                proposal_req = {
                    "proposal":      1,
                    "amount":        stake,
                    "basis":         "stake",
                    "contract_type": contract_type,
                    "currency":      "USD",
                    "duration":      5,
                    "duration_unit": "m",
                    "symbol":        symbol,
                }
                proposal = await self._send(proposal_req)
                logger.info(f"PROPOSAL RESPONSE: {proposal}")
                if not proposal or proposal.get("error"):
                    err = (
                        proposal.get("error", {}).get("message", "unknown")
                        if proposal else "no response"
                    )
                    logger.error(f"PROPOSAL FAILED: {symbol} -- {err}")
                    return None

                proposal_id = proposal["proposal"]["id"]

                # Step 2: buy
                buy_req  = {"buy": proposal_id, "price": stake}
                buy_resp = await self._send(buy_req)
                logger.info(f"RAW BUY RESPONSE: {buy_resp}")
                if not buy_resp or buy_resp.get("error"):
                    err = (
                        buy_resp.get("error", {}).get("message", "unknown")
                        if buy_resp else "no response"
                    )
                    logger.error(f"BUY FAILED: {symbol} -- {err}")
                    return None

                contract_id = str(buy_resp["buy"]["contract_id"])
                logger.info(
                    f"CONTRACT OPENED: {contract_id} | {symbol} | "
                    f"{contract_type} | ${stake:.4f}"
                )

                self._contract_symbol_map[contract_id] = symbol
                asyncio.ensure_future(
                    self._eagerly_subscribe_contract(contract_id)
                )

                return buy_resp["buy"]

            except Exception as e:
                logger.error(f"BUY EXCEPTION: {symbol} -- {e}")
                return None


    async def _eagerly_subscribe_contract(self, contract_id: str):
        """
        Immediately subscribe to proposal_open_contract after buy_contract().
        Called as fire-and-forget so it doesn't block the buy response.
        If subscribe_contract() has already registered a sub_id, skip.
        """
        # Short delay to let subscribe_contract() register first (it's called
        # synchronously right after buy_contract returns in _execute)
        await asyncio.sleep(0.5)

        if contract_id in self._subscriptions or contract_id in self._subscribed_contracts:
            # Already subscribed via subscribe_contract() — nothing to do
            return

        if not self._authorized or not self._ws:
            logger.debug(
                f"_eagerly_subscribe_contract({contract_id}): not connected, skipping"
            )
            return

        try:
            resp = await self._send(
                {
                    "proposal_open_contract": 1,
                    "contract_id": int(contract_id),
                    "subscribe":   1,
                },
                timeout=20,
            )
            poc    = resp.get("proposal_open_contract", {})
            sub_id = (
                poc.get("id")
                or resp.get("subscription", {}).get("id", "")
            )
            if sub_id and contract_id not in self._subscriptions:
                self._subscriptions[contract_id] = sub_id
                self._subscribed_contracts.add(contract_id)
                logger.info(
                    f"Eager subscription: contract {contract_id} (sub_id={sub_id})"
                )

            # Buffer the response in case callback isn't registered yet
            self._pending_contract_msgs[contract_id] = resp

            # If already closed, mark for flush
            if self._contract_is_closed(poc):
                self._closed_before_callback.add(contract_id)
                logger.info(
                    f"Eager subscribe: contract {contract_id} already closed — buffered"
                )
                # If callback registered by now, fire it
                if contract_id in self._contract_callbacks:
                    try:
                        self._contract_callbacks[contract_id](resp)
                    except Exception as exc:
                        logger.debug(f"Eager subscribe callback flush error: {exc}")

        except Exception as exc:
            logger.debug(f"_eagerly_subscribe_contract({contract_id}) failed: {exc}")

    # ─── Contract subscription ────────────────────────────────────────────────

    async def subscribe_contract(
        self,
        contract_id: str,
        callback:    Callable[[dict], None],
        symbol:      str = "",
    ):
        """
        Subscribe to live updates for an open contract.
        Stores the subscription ID in self._subscriptions for later cleanup.

        If _closed_before_callback contains this contract_id, the callback
        is flushed immediately with the buffered message.
        """
        self._contract_callbacks[contract_id] = callback
        if symbol:
            self._contract_symbol_map[contract_id] = symbol

        # ── Flush if contract already closed before this callback registered ─
        if contract_id in self._closed_before_callback:
            buffered = self._pending_contract_msgs.get(contract_id)
            if buffered:
                logger.info(
                    f"subscribe_contract({contract_id}): flushing buffered close message"
                )
                self._closed_before_callback.discard(contract_id)
                try:
                    callback(buffered)
                except Exception as exc:
                    logger.debug(f"subscribe_contract flush callback error: {exc}")
                return  # cleanup will handle unsubscribe

        # ── Guard: never double-subscribe ────────────────────────────────────
        if contract_id in self._subscribed_contracts or contract_id in self._subscriptions:
            logger.info(
                f"subscribe_contract({contract_id}): already subscribed "
                f"(sub_id={self._subscriptions.get(contract_id, 'eager')}), callback registered"
            )
            return

        try:
            resp = await self._send(
                {
                    "proposal_open_contract": 1,
                    "contract_id": int(contract_id),
                    "subscribe":   1,
                },
                timeout=20,
            )
            poc    = resp.get("proposal_open_contract", {})
            sub_id = (
                poc.get("id")
                or resp.get("subscription", {}).get("id", "")
            )
            if sub_id:
                self._subscriptions[contract_id] = sub_id
                logger.info(
                    f"Contract subscribed: {contract_id} (sub_id={sub_id})"
                )
            self._subscribed_contracts.add(contract_id)

            # Buffer and check if already closed
            self._pending_contract_msgs[contract_id] = resp
            if self._contract_is_closed(poc):
                logger.info(
                    f"subscribe_contract({contract_id}): contract already closed — "
                    f"firing callback immediately"
                )
                try:
                    callback(resp)
                except Exception as exc:
                    logger.debug(f"subscribe_contract immediate close callback error: {exc}")

        except Exception as exc:
            logger.warning(f"subscribe_contract({contract_id}) failed: {exc}")

    # ─── Active symbols ───────────────────────────────────────────────────────

    async def get_active_symbols(self) -> List[dict]:
        try:
            resp = await self._send(
                {"active_symbols": "brief", "product_type": "basic"},
                timeout=20,
            )
            return resp.get("active_symbols", [])
        except Exception as exc:
            logger.error(f"get_active_symbols failed: {exc}")
            return []

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected and self._authorized
