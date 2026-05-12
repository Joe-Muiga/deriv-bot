"""
deriv_client.py – Async Deriv WebSocket client.

Handles:
  • Connection + authorisation
  • Balance subscription
  • Historical candle fetching (ticks_history)
  • Live tick streaming (ticks)
  • Contract buying (Rise / Fall binary options)
  • Contract monitoring (buy subscription)
  • Auto-reconnect with exponential back-off
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Optional, List, Any

import websockets
from websockets.exceptions import ConnectionClosed

import config

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY = 60   # seconds


class DerivClient:

    def __init__(self):
        self._ws: Optional[Any] = None
        self._connected: bool   = False
        self._authorized: bool  = False

        # Pending requests: req_id → asyncio.Future
        self._pending: Dict[int, asyncio.Future] = {}
        self._req_id_counter: int = 1

        # Subscriptions: subscription_id → callback(data)
        self._tick_callbacks: Dict[str, Callable]  = {}
        self._subscription_map: Dict[str, str] = {}  # symbol → sub_id

        # Balance tracking
        self._balance: float = 0.0
        self._balance_callbacks: List[Callable] = []

        # Contract result callbacks: contract_id → callback
        self._contract_callbacks: Dict[str, Callable] = {}

        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ─── Connection ───────────────────────────────────────────────────────────

    async def connect(self):
        """Connect, authorize, and start the message-dispatch loop."""
        self._connected.set()
        self._loop = asyncio.get_event_loop()
        retry_delay = 2
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
                    retry_delay     = 2
                    logger.info("WebSocket connected ✓")

                    await self._authorize()
                    await self._subscribe_balance()
                    await self._dispatch_loop()

            except ConnectionClosed as exc:
                logger.warning(f"Connection closed: {exc}. Retrying in {retry_delay}s …")
            except Exception as exc:
                logger.error(f"WebSocket error: {exc}. Retrying in {retry_delay}s …")
            finally:
                self._connected  = False
                self._authorized = False
                self._ws         = None

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)

    # ─── Message dispatch ─────────────────────────────────────────────────────

    async def _dispatch_loop(self):
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
                await self._handle(msg)
            except Exception as exc:
                logger.debug(f"Dispatch error: {exc}")

    async def _handle(self, msg: dict):
        logger.debug(f"RAW MSG: {msg}")
        msg_type = msg.get("msg_type", "")
        req_id   = msg.get("req_id")
        error    = msg.get("error")

        if error:
            logger.warning(f"Deriv API error: {error.get('message')} "
                           f"(code={error.get('code')}) | msg_type={msg_type}")

        # Resolve pending request futures
        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                if error:
                    fut.set_exception(RuntimeError(error.get("message", "Unknown error")))
                else:
                    fut.set_result(msg)
            return

        # Balance updates
        if msg_type == "balance":
            balance_data = msg.get("balance", {})
            self._balance = float(balance_data.get("balance", self._balance))
            for cb in self._balance_callbacks:
                try:
                    cb(self._balance)
                except Exception:
                    pass

        # Tick updates
        elif msg_type == "tick":
            tick = msg.get("tick", {})
            sym  = tick.get("symbol", "")
            sub_id = tick.get("id", "")
            for key, cb in self._tick_callbacks.items():
                if key == sym or key == sub_id:
                    try:
                        cb(tick)
                    except Exception as exc:
                        logger.debug(f"Tick callback error: {exc}")

        # Contract updates
        elif msg_type in ("proposal_open_contract", "buy"):
            contract = msg.get("proposal_open_contract") or msg.get("buy", {})
            cid = str(contract.get("contract_id", ""))
            if cid in self._contract_callbacks:
                try:
                    self._contract_callbacks[cid](msg)
                except Exception:
                    pass

    # ─── Request helper ───────────────────────────────────────────────────────

    async def _send(self, payload: dict, timeout: float = 30.0) -> dict:
        """Send a request and await the response."""
        if not self._ws or not self._connected:
            raise RuntimeError("Not connected")

        req_id             = self._req_id_counter
        self._req_id_counter += 1
        payload["req_id"]  = req_id

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
        # FIX 1: "authorie" → "authorize" (typo in key name)
        payload = {"authorize": config.DERIV_API_TOKEN, "req_id": 1}
        await self._ws.send(json.dumps(payload))
        # FIX 2: self.ws → self._ws (missing underscore caused AttributeError)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
        msg = json.loads(raw)
        account = msg.get("authorize", {})
        self._balance = float(account.get("balance", 0))
        self._authorized = True
        self._req_id_counter = 2
        logger.info(f"Authorized | Balance: ${self._balance:.4f}")

    async def _subscribe_balance(self):
        await self._send({"balance": 1, "req_id": 2})
        logger.info("Balance subscription active")

    def on_balance(self, callback: Callable[[float], None]):
        """Register a callback for balance updates."""
        self._balance_callbacks.append(callback)

    @property
    def balance(self) -> float:
        return self._balance

    # ─── Historical candles ───────────────────────────────────────────────────

    async def get_candles(self, symbol: str, granularity: int,
                          count: int = 100) -> List[dict]:
        """
        Fetch historical OHLCV bars via ticks_history.
        Returns list of {epoch, open, high, low, close} dicts.
        """
        await self._connected.wait()                    
        try:
            resp = await self._send({
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count":       count,
                "end":         "latest",
                "granularity": granularity,
                "style":       "candles",
            }, timeout=20)
            candles = resp.get("candles", [])
            logger.debug(f"Got {len(candles)} candles for {symbol} "
                         f"(gran={granularity}s)")
            return candles
        except Exception as exc:
            logger.warning(f"get_candles({symbol}) failed: {exc}")
            return []

    # ─── Live tick subscription ───────────────────────────────────────────────

    async def subscribe_ticks(self, symbol: str,
                              callback: Callable[[dict], None]) -> str:
        """Subscribe to live ticks for `symbol`. Returns subscription id."""
        if symbol in self._subscription_map:
            # Already subscribed – just update callback
            self._tick_callbacks[symbol] = callback
            return self._subscription_map[symbol]

        try:
            resp = await self._send({
                "ticks":     symbol,
                "subscribe": 1,
            })
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

    # ─── Trade execution ──────────────────────────────────────────────────────

    async def buy_contract(self,
                           symbol:    str,
                           direction: str,    # "LONG" | "SHORT"
                           stake:     float,
                           duration:  int  = 5,
                           dur_unit:  str  = "m") -> Optional[dict]:
        """
        Buy a Rise (CALL) or Fall (PUT) binary option.
        Returns the buy response dict, or None on failure.
        """
        contract_type = "CALL" if direction == "LONG" else "PUT"
        payload = {
            "buy":   "1",
            "price": stake,
            "parameters": {
                "amount":        stake,
                "basis":         "stake",
                "contract_type": contract_type,
                "currency":      config.DERIV_CURRENCY,
                "duration":      duration,
                "duration_unit": dur_unit,
                "symbol":        symbol,
            },
        }
        try:
            resp = await self._send(payload, timeout=15)
            buy_info = resp.get("buy", {})
            cid      = str(buy_info.get("contract_id", ""))
            logger.info(f"BUY {contract_type} {symbol} | stake=${stake:.2f} | "
                        f"contract_id={cid} | "
                        f"buy_price={buy_info.get('buy_price')}")
            return buy_info
        except Exception as exc:
            logger.error(f"buy_contract failed: {exc}")
            return None

    async def subscribe_contract(self, contract_id: str,
                                 callback: Callable[[dict], None]):
        """Subscribe to contract updates for profit/loss tracking."""
        self._contract_callbacks[contract_id] = callback
        try:
            await self._send({
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
                "subscribe":   1,
            })
        except Exception as exc:
            logger.warning(f"subscribe_contract({contract_id}) failed: {exc}")

    async def get_active_symbols(self) -> List[dict]:
        """Return list of currently active/tradeable symbols."""
        try:
            resp = await self._send({
                "active_symbols": "brief",
                "product_type":   "basic",
            }, timeout=20)
            return resp.get("active_symbols", [])
        except Exception as exc:
            logger.error(f"get_active_symbols failed: {exc}")
            return []

    @property
    def is_connected(self) -> bool:
        return self._connected and self._authorized
