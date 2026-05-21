"""
deriv_client.py – Async Deriv WebSocket client.
v11 — Aligned with v16 bot. Preserves all v10 guarantees.

DIRECTION MAPPING (VERIFIED CORRECT — DO NOT SWAP):
  direction="LONG"  → contract_type="CALL"  → wins if price RISES
  direction="SHORT" → contract_type="PUT"   → wins if price FALLS
  Logged before every placement.

CONTRACT CLOSURE GUARANTEE:
  buy_contract() immediately subscribes via proposal_open_contract.
  Fallback poller runs every 30s for all open contracts.
  Re-subscribes all open contracts on reconnect.
  Never raises from buy_contract() — always returns None on failure.

buy_contract() returns the contract_id string on success, None on failure.

register_contract_callback(contract_id, callback):
  Registers a callback to fire when contract closes.
  Used by bot_engine instead of subscribe_contract().
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Set, Any

import websockets
from websockets.exceptions import ConnectionClosed

import config

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY = 60

_BOOM_CRASH_PREFIXES = ("BOOM", "CRASH")
_VOLATILITY_PREFIXES = ("R_", "1HZ")

_DEFAULT_RECONNECT_INTERVAL = 5
_DEFAULT_MAX_RECONNECTS     = 10
_FALLBACK_POLL_INTERVAL     = 30
_BALANCE_CACHE_TTL          = 30.0


def _is_boom_crash(symbol: str) -> bool:
    return any(symbol.upper().startswith(p) for p in _BOOM_CRASH_PREFIXES)


def _is_volatility_index(symbol: str) -> bool:
    s = symbol.upper()
    return any(s.startswith(p) for p in _VOLATILITY_PREFIXES)


class DerivClient:

    def __init__(self):
        self._ws          : Optional[Any]           = None
        self._ready       : asyncio.Event            = asyncio.Event()
        self._connected   : bool                     = False
        self._authorized  : bool                     = False

        self._pending     : Dict[int, asyncio.Future] = {}
        self._req_id_counter : int = 10

        self._tick_callbacks    : Dict[str, Callable]       = {}
        self._subscription_map  : Dict[str, str]            = {}

        # contract_id → proposal subscription_id
        self._subscriptions         : Dict[str, str]    = {}
        self._subscribed_contracts  : Set[str]          = set()
        self._contract_symbol_map   : Dict[str, str]    = {}

        self._balance             : float            = 0.0
        self._balance_ts          : float            = 0.0
        self._balance_callbacks   : List[Callable]   = []

        self._contract_callbacks        : Dict[str, Callable] = {}
        self._pending_contract_msgs     : Dict[str, dict]     = {}
        self._closed_before_callback    : Set[str]            = set()

        self._loop : Optional[asyncio.AbstractEventLoop] = None

        logger.info(
            "Direction mapping: LONG → CALL (price rises) | SHORT → PUT (price falls)")

    # ─── Connection ───────────────────────────────────────────────────────────

    async def connect(self):
        self._loop = asyncio.get_event_loop()
        asyncio.ensure_future(self._fallback_poll_loop())

        reconnect_interval = getattr(config, "WEBSOCKET_RECONNECT_INTERVAL", _DEFAULT_RECONNECT_INTERVAL)
        max_reconnects     = getattr(config, "WEBSOCKET_MAX_RECONNECTS",     _DEFAULT_MAX_RECONNECTS)
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
                    attempt         = 0
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
                    f"(attempt {attempt}/{max_label}) | reason={exc}")
            except Exception as exc:
                attempt += 1
                logger.error(
                    f"WEBSOCKET DISCONNECTED — reconnecting in {reconnect_interval}s "
                    f"(attempt {attempt}/{max_label}) | error={exc}")
            finally:
                self._ready.clear()
                self._connected  = False
                self._authorized = False
                self._ws         = None
                self._pending.clear()

            if max_reconnects > 0 and attempt >= max_reconnects:
                logger.critical("WEBSOCKET RECONNECT FAILED — bot halting")
                raise ConnectionError(
                    f"WebSocket failed after {max_reconnects} reconnect attempts.")

            await asyncio.sleep(reconnect_interval)

    # ─── Post-reconnect resubscription ───────────────────────────────────────

    async def _resubscribe_open_contracts(self):
        targets = self._subscribed_contracts.copy()
        if not targets:
            return
        logger.info(f"Re-subscribing {len(targets)} open contract(s) after reconnect")
        await asyncio.gather(*[
            self._reattach_contract(cid, self._contract_callbacks.get(cid))
            for cid in targets
        ], return_exceptions=True)

    async def _reattach_contract(self, contract_id: str, callback: Optional[Callable]):
        try:
            resp = await self._send({
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
                "subscribe":   1,
            })
            poc    = resp.get("proposal_open_contract", {})
            sub_id = poc.get("id") or resp.get("subscription", {}).get("id", "")
            if sub_id:
                self._subscriptions[contract_id] = sub_id
            self._subscribed_contracts.add(contract_id)
            logger.info(f"Re-subscribed contract {contract_id} (sub_id={sub_id})")
            if self._contract_is_closed(poc) and callback is not None:
                logger.info(f"Contract {contract_id} already closed on reconnect — firing callback")
                try:
                    callback(resp)
                except Exception as exc:
                    logger.debug(f"_reattach_contract callback error ({contract_id}): {exc}")
        except Exception as exc:
            logger.warning(f"_reattach_contract({contract_id}) failed: {exc}")

    # ─── Dispatch loop ────────────────────────────────────────────────────────

    async def _dispatch_loop(self):
        async for raw in self._ws:
            try:
                await self._handle(json.loads(raw))
            except Exception as exc:
                logger.debug(f"Dispatch error: {exc}")

    @staticmethod
    def _contract_is_closed(poc: dict) -> bool:
        return bool(
            poc.get("is_sold")
            or poc.get("is_expired")
            or poc.get("status") in ("sold", "won", "lost")
        )

    async def _handle(self, msg: dict):
        msg_type = msg.get("msg_type", "")
        req_id   = msg.get("req_id")
        error    = msg.get("error")

        if error:
            logger.warning(
                f"Deriv API error: {error.get('message')} "
                f"(code={error.get('code')}) | msg_type={msg_type}")

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
                        f"{error.get('code', 'ERR')}: {error.get('message', 'Unknown error')}"))
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

            self._pending_contract_msgs[cid] = msg
            is_closed = self._contract_is_closed(contract)

            if is_closed:
                asyncio.ensure_future(self._cleanup_contract_subscription(cid))

            if cid in self._contract_callbacks:
                if is_closed or msg_type == "proposal_open_contract":
                    try:
                        self._contract_callbacks[cid](msg)
                    except Exception as exc:
                        logger.debug(f"Contract callback error ({cid}): {exc}")
            elif is_closed:
                self._closed_before_callback.add(cid)

    # ─── Fallback poller (30s) ────────────────────────────────────────────────

    async def _fallback_poll_loop(self):
        while True:
            await asyncio.sleep(_FALLBACK_POLL_INTERVAL)
            if not self._authorized or not self._ws:
                continue
            for cid, callback in list(self._contract_callbacks.items()):
                try:
                    resp = await self._send({
                        "proposal_open_contract": 1,
                        "contract_id": int(cid),
                    }, timeout=15)
                    poc = resp.get("proposal_open_contract", {})
                    if self._contract_is_closed(poc):
                        logger.info(f"Fallback poll: contract {cid} is closed — triggering callback")
                        try:
                            callback(resp)
                        except Exception as exc:
                            logger.debug(f"Fallback poll callback error ({cid}): {exc}")
                except Exception as exc:
                    logger.debug(f"Fallback poll failed for {cid}: {exc}")

    # ─── Subscription cleanup ─────────────────────────────────────────────────

    async def _cleanup_contract_subscription(self, contract_id: str):
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
                logger.debug(f"forget({sub_id}) for {symbol}/{contract_id} failed: {exc}")
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

    # ─── Auth ─────────────────────────────────────────────────────────────────

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
            f"Authorized ✓ | Account: {account.get('loginid')} | Balance: ${self._balance:.4f}")
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
            logger.warning(f"Balance subscription failed: {exc} — will use polling fallback")

    # ─── Balance ─────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        now = time.monotonic()
        if (now - self._balance_ts) < _BALANCE_CACHE_TTL:
            return self._balance
        if not self._authorized or not self._ws:
            logger.warning(f"BALANCE FETCH FAILED — using cached: ${self._balance:.2f} (not connected)")
            return self._balance
        try:
            resp    = await self._send({"balance": 1}, timeout=15)
            new_bal = float(resp.get("balance", {}).get("balance", self._balance))
            if new_bal != self._balance:
                logger.info(f"get_balance: ${self._balance:.4f} → ${new_bal:.4f}")
            self._balance    = new_bal
            self._balance_ts = time.monotonic()
            return self._balance
        except Exception as exc:
            logger.warning(f"BALANCE FETCH FAILED — using cached: ${self._balance:.2f} | error={exc}")
            return self._balance

    def on_balance(self, callback: Callable[[float], None]):
        self._balance_callbacks.append(callback)

    @property
    def balance(self) -> float:
        return self._balance

    # ─── Candles ─────────────────────────────────────────────────────────────

    async def get_candles(self, symbol: str, granularity: int, count: int = 100) -> List[dict]:
        """
        Returns list[dict] with keys: open, high, low, close, epoch.
        On failure: returns [], logs CANDLE FETCH FAILED.
        """
        await self._ready.wait()
        try:
            resp = await self._send({
                "ticks_history":     symbol,
                "adjust_start_time": 1,
                "count":             count,
                "end":               "latest",
                "granularity":       granularity,
                "style":             "candles",
            }, timeout=20)
            return [
                {
                    "open":  float(c.get("open",  0)),
                    "high":  float(c.get("high",  0)),
                    "low":   float(c.get("low",   0)),
                    "close": float(c.get("close", 0)),
                    "epoch": int(c.get("epoch",   0)),
                }
                for c in resp.get("candles", [])
            ]
        except Exception as exc:
            logger.warning(f"CANDLE FETCH FAILED: {symbol} gran={granularity} | error={exc}")
            return []

    # ─── Market-open check ────────────────────────────────────────────────────

    async def _check_market_open(self, symbol: str) -> bool:
        if _is_volatility_index(symbol):
            return True
        try:
            resp    = await self._send({"active_symbols": "brief", "product_type": "basic"}, timeout=20)
            symbols = resp.get("active_symbols", [])
            for s in symbols:
                if s.get("symbol") == symbol:
                    return bool(s.get("exchange_is_open", 0))
            logger.warning(f"SKIPPED: {symbol} market_closed (symbol not in active list)")
            return False
        except Exception as exc:
            logger.warning(f"SKIPPED: {symbol} market_closed (check failed: {exc})")
            return False

    # ─── Trade execution ──────────────────────────────────────────────────────

    async def buy_contract(
        self,
        symbol        : str,
        direction     : str,
        stake         : float,
        duration      : int = 5,
        duration_unit : str = "m",
    ) -> Optional[str]:
        """
        Buy a binary option. Returns contract_id string on success, None on any failure.
        LONG → CALL, SHORT → PUT. Verified and logged before every placement.
        Never raises.
        """
        # 1. Direction → contract_type
        if direction == "LONG":
            contract_type = "CALL"
        elif direction == "SHORT":
            contract_type = "PUT"
        else:
            logger.error(f"buy_contract: unknown direction '{direction}' on {symbol} — aborting")
            return None

        logger.info(f"LONG→CALL / SHORT→PUT | Signal: {direction} → Contract: {contract_type}")

        # 2. Pre-placement log
        logger.info(
            f"PLACING {contract_type} on {symbol} | "
            f"stake=${stake:.2f} | duration={duration}{duration_unit}")

        # 3. Market open check (Boom/Crash only)
        if _is_boom_crash(symbol):
            try:
                market_open = await self._check_market_open(symbol)
            except Exception:
                market_open = False
            if not market_open:
                logger.warning(f"SKIPPED: {symbol} market_closed")
                return None

        # 4. Send buy
        payload = {
            "buy":   "1",
            "price": stake,
            "parameters": {
                "amount":        stake,
                "basis":         "stake",
                "contract_type": contract_type,
                "currency":      config.DERIV_CURRENCY,
                "duration":      duration,
                "duration_unit": duration_unit,
                "symbol":        symbol,
            },
        }

        try:
            resp     = await self._send(payload, timeout=15)
            error    = resp.get("error")
            if error:
                logger.error(
                    f"PLACEMENT FAILED: {symbol} reason={error.get('code', 'unknown')}: "
                    f"{error.get('message', 'unknown error')}")
                return None

            buy_info = resp.get("buy", {})
            cid      = str(buy_info.get("contract_id", ""))
            if not cid:
                logger.error(f"PLACEMENT FAILED: {symbol} reason=proposal_rejected (no contract_id)")
                return None

            self._contract_symbol_map[cid] = symbol
            logger.info(
                f"CONFIRM | symbol={symbol} | direction={direction} → {contract_type} | "
                f"stake=${stake:.2f} | contract_id={cid} | "
                f"buy_price={buy_info.get('buy_price')} | balance=${self._balance:.4f}")

            asyncio.ensure_future(self._eagerly_subscribe_contract(cid))
            return cid

        except TimeoutError:
            logger.error(f"PLACEMENT FAILED: {symbol} reason=timeout")
            return None
        except RuntimeError as exc:
            err_str = str(exc)
            if "ContractBuyValidationError" in err_str or "proposal" in err_str.lower():
                logger.error(f"PLACEMENT FAILED: {symbol} reason=proposal_rejected | detail={err_str}")
            else:
                logger.error(f"PLACEMENT FAILED: {symbol} reason={err_str}")
            return None
        except Exception as exc:
            logger.error(f"PLACEMENT FAILED: {symbol} reason={exc}")
            return None

    # ─── Contract callback registration ──────────────────────────────────────

    def register_contract_callback(self, contract_id: str, callback: Callable[[dict], None]) -> None:
        """Register a callback to fire when contract closes. Flushes buffered close if already done."""
        self._contract_callbacks[contract_id] = callback
        if contract_id in self._closed_before_callback:
            buffered = self._pending_contract_msgs.get(contract_id)
            if buffered:
                logger.info(f"register_contract_callback({contract_id}): flushing buffered close")
                self._closed_before_callback.discard(contract_id)
                try:
                    callback(buffered)
                except Exception as exc:
                    logger.debug(f"register_contract_callback flush error: {exc}")

    # ─── Eager subscribe ──────────────────────────────────────────────────────

    async def _eagerly_subscribe_contract(self, contract_id: str):
        await asyncio.sleep(0.5)
        if contract_id in self._subscriptions or contract_id in self._subscribed_contracts:
            return
        if not self._authorized or not self._ws:
            return
        try:
            resp = await self._send({
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
                "subscribe":   1,
            }, timeout=20)
            poc    = resp.get("proposal_open_contract", {})
            sub_id = poc.get("id") or resp.get("subscription", {}).get("id", "")
            if sub_id and contract_id not in self._subscriptions:
                self._subscriptions[contract_id] = sub_id
                self._subscribed_contracts.add(contract_id)
                logger.info(f"Eager subscription: contract {contract_id} (sub_id={sub_id})")
            self._pending_contract_msgs[contract_id] = resp
            if self._contract_is_closed(poc):
                self._closed_before_callback.add(contract_id)
                if contract_id in self._contract_callbacks:
                    try:
                        self._contract_callbacks[contract_id](resp)
                    except Exception as exc:
                        logger.debug(f"Eager subscribe callback flush error: {exc}")
        except Exception as exc:
            logger.debug(f"_eagerly_subscribe_contract({contract_id}) failed: {exc}")

    # ─── Force check ─────────────────────────────────────────────────────────

    async def force_check_contract(self, contract_id: str) -> dict:
        try:
            resp = await self._send({"proposal_open_contract": 1, "contract_id": contract_id})
            return resp.get("proposal_open_contract", {})
        except Exception as exc:
            logger.error(f"force_check_contract({contract_id}): {exc}")
            return {}

    # ─── Tick subscriptions ───────────────────────────────────────────────────

    async def subscribe_ticks(self, symbol: str, callback: Callable[[dict], None]) -> str:
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

    # ─── Active symbols ───────────────────────────────────────────────────────

    async def get_active_symbols(self) -> List[dict]:
        try:
            resp = await self._send({"active_symbols": "brief", "product_type": "basic"}, timeout=20)
            return resp.get("active_symbols", [])
        except Exception as exc:
            logger.error(f"get_active_symbols failed: {exc}")
            return []

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected and self._authorized
