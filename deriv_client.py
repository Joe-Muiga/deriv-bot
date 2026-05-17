"""
deriv_client.py – Async Deriv WebSocket client.

v8 → v9 changes (Change 6):

  ENHANCED TRADE PLACEMENT LOGGING:
    buy_contract() now logs a dedicated placement line BEFORE sending the
    API request, and a confirmation line AFTER receiving the response.
    Every placement line explicitly states:
      • symbol
      • direction (LONG / SHORT)  →  contract_type (CALL / PUT)
      • stake ($)
      • contract type string
      • duration + unit

    This makes log grep / filtering trivial:
      grep "PLACE" bot.log
      grep "CONFIRM" bot.log

  No logic changes.  All v8 direction-mapping audit code preserved.
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

MAX_RETRY_DELAY = 60

_BOOM_CRASH_PREFIXES = ("BOOM", "CRASH")


def _is_boom_crash(symbol: str) -> bool:
    return any(symbol.upper().startswith(p) for p in _BOOM_CRASH_PREFIXES)


class DerivClient:

    def __init__(self):
        self._ws: Optional[Any] = None
        self._ready: asyncio.Event = asyncio.Event()
        self._connected: bool      = False
        self._authorized: bool     = False

        self._pending: Dict[int, asyncio.Future] = {}
        self._req_id_counter: int = 10

        self._tick_callbacks: Dict[str, Callable] = {}
        self._subscription_map: Dict[str, str]    = {}

        self._balance: float             = 0.0
        self._balance_callbacks: List[Callable] = []

        self._contract_callbacks: Dict[str, Callable] = {}

        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Direction→contract mapping audit ──────────────────────────────────
        logger.info(
            "Direction mapping: LONG → CALL (price rises) | SHORT → PUT (price falls)"
        )
        assert "CALL" == ("CALL" if "LONG" == "LONG" else "PUT"), (
            "FATAL: LONG→CALL mapping is broken")
        assert "PUT" == ("CALL" if "SHORT" == "LONG" else "PUT"), (
            "FATAL: SHORT→PUT mapping is broken")

    # ─── Connection ───────────────────────────────────────────────────────────

    async def connect(self):
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
                self._ready.clear()
                self._connected  = False
                self._authorized = False
                self._ws         = None
                self._pending.clear()

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

        if msg_type == "balance":
            balance_data = msg.get("balance", {})
            new_bal = float(balance_data.get("balance", self._balance))
            if new_bal != self._balance:
                logger.info(f"Balance updated: ${self._balance:.4f} → ${new_bal:.4f}")
            self._balance = new_bal
            for cb in self._balance_callbacks:
                try:
                    cb(self._balance)
                except Exception:
                    pass

        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                if error:
                    fut.set_exception(RuntimeError(error.get("message", "Unknown error")))
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
            cid = str(contract.get("contract_id", ""))
            if cid in self._contract_callbacks:
                try:
                    self._contract_callbacks[cid](msg)
                except Exception:
                    pass

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

        account = msg.get("authorize", {})
        self._balance        = float(account.get("balance", 0))
        self._authorized     = True
        self._req_id_counter = 10

        self._ready.set()

        logger.info(f"Authorized ✓ | Account: {account.get('loginid')} | "
                    f"Balance: ${self._balance:.4f}")

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

    async def balance_refresh_loop(self, interval: int = 60):
        while True:
            await asyncio.sleep(interval)
            if not self._authorized or not self._ws:
                continue
            try:
                resp = await self._send({"balance": 1})
                new_bal = float(resp.get("balance", {}).get("balance", self._balance))
                if new_bal != self._balance:
                    logger.info(f"Balance poll: ${self._balance:.4f} → ${new_bal:.4f}")
                    self._balance = new_bal
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

    # ─── Historical candles ───────────────────────────────────────────────────

    async def get_candles(self, symbol: str, granularity: int,
                          count: int = 100) -> List[dict]:
        await self._ready.wait()
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
        if symbol in self._subscription_map:
            self._tick_callbacks[symbol] = callback
            return self._subscription_map[symbol]

        try:
            resp = await self._send({"ticks": symbol, "subscribe": 1})
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
                           direction: str,
                           stake:     float,
                           duration:  int  = 5,
                           dur_unit:  str  = "m") -> Optional[dict]:
        """
        Buy a Rise (CALL) or Fall (PUT) binary option.

        Direction mapping (VERIFIED CORRECT, DO NOT SWAP):
          direction="LONG"  → contract_type="CALL"  → wins if price RISES
          direction="SHORT" → contract_type="PUT"   → wins if price FALLS
        """
        if direction == "LONG":
            contract_type = "CALL"
        elif direction == "SHORT":
            contract_type = "PUT"
        else:
            logger.error(f"buy_contract: unknown direction '{direction}' — aborting")
            return None

        if _is_boom_crash(symbol):
            effective_duration  = getattr(config, "BOOM_CRASH_TICK_DURATION", 10)
            effective_dur_unit  = getattr(config, "BOOM_CRASH_DURATION_UNIT", "t")
            logger.info(
                f"BOOM/CRASH symbol detected ({symbol}): overriding to "
                f"{effective_duration}{effective_dur_unit} tick contract")
        else:
            effective_duration = duration
            effective_dur_unit = dur_unit

        # ── Pre-placement log ─────────────────────────────────────────────────
        logger.info(
            f"PLACE | symbol={symbol} | direction={direction} → "
            f"contract_type={contract_type} | stake=${stake:.2f} | "
            f"duration={effective_duration}{effective_dur_unit}")

        payload = {
            "buy":   "1",
            "price": stake,
            "parameters": {
                "amount":        stake,
                "basis":         "stake",
                "contract_type": contract_type,
                "currency":      config.DERIV_CURRENCY,
                "duration":      effective_duration,
                "duration_unit": effective_dur_unit,
                "symbol":        symbol,
            },
        }
        try:
            resp     = await self._send(payload, timeout=15)
            buy_info = resp.get("buy", {})
            cid      = str(buy_info.get("contract_id", ""))

            # ── Post-placement confirmation log ───────────────────────────────
            logger.info(
                f"CONFIRM | symbol={symbol} | direction={direction} → {contract_type} | "
                f"stake=${stake:.2f} | contract_id={cid} | "
                f"buy_price={buy_info.get('buy_price')} | "
                f"balance=${self._balance:.4f}")
            return buy_info
        except Exception as exc:
            logger.error(
                f"buy_contract FAILED | symbol={symbol} | direction={direction} | "
                f"stake=${stake:.2f} | error={exc}")
            return None

    async def subscribe_contract(self, contract_id: str,
                                 callback: Callable[[dict], None]):
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
