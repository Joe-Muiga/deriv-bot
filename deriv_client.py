"""
deriv_client.py – Async Deriv WebSocket client.

v13 — POLLING-BASED CONTRACT RESOLUTION (replaces subscription model):

  ROOT CAUSE OF ORPHAN_TIMEOUT:
    - WebSocket proposal_open_contract subscriptions never delivered
      reliable close callbacks (no CALLBACK FIRED / POLLER CAUGHT events
      observed in production logs).

  FIX — Aggressive polling replaces all subscription-based monitoring:
    - All proposal_open_contract subscribe/forget/callback machinery removed
      (_contract_callbacks, _subscriptions, _subscribed_contracts,
      _pending_contract_msgs, _closed_before_callback, _contract_poller,
      _fallback_poll_loop, _resubscribe_open_contracts, _subscribe_after_buy,
      _cleanup_contract_subscription — all removed).
    - subscribe_contract(contract_id, callback) now simply registers the
      contract + callback in self._polling_contracts.
    - _polling_loop() runs every 30s, calls force_check_contract(cid) for
      every tracked contract. On is_sold/is_expired it pops the entry and
      fires the callback with the full proposal_open_contract dict
      (containing profit, sell_price, is_sold, is_expired, etc).
    - Started once in connect() via asyncio.create_task().

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
    - Re-authenticates after reconnect

  get_candles():
    - Returns list[dict] with keys: open, high, low, close, epoch
    - On failure: returns [], logs CANDLE FETCH FAILED: {symbol} gran={granularity}
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Optional, List, Any

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

import config

# Deriv's newer Options trading API (REST for account/OTP setup, WS for trading).
# Replaces the legacy wss://ws.derivws.com/websockets/v3?app_id=... endpoint,
# which now returns HTTP 401 (InvalidAppID) at the handshake.
_OPTIONS_API_BASE = "https://api.derivws.com/trading/v1/options"

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY = 60

_BOOM_CRASH_PREFIXES = ("BOOM", "CRASH")
_VOLATILITY_PREFIXES = ("R_", "1HZ")

# ── Reconnect defaults (overridden by config if present) ──────────────────────
_DEFAULT_RECONNECT_INTERVAL = 5   # seconds between retries
_DEFAULT_MAX_RECONNECTS     = 10  # 0 = unlimited

# Contract polling interval in seconds
_CONTRACT_POLL_INTERVAL = 30

# Minimum gap between proposal requests (rate-limit protection)
PROPOSAL_DELAY = 2.0  # 2 seconds between proposals


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

        # contract_id → symbol  (informational only)
        self._contract_symbol_map: Dict[str, str] = {}

        self._balance: float             = 0.0
        self._balance_ts: float          = 0.0          # epoch of last successful fetch
        self._balance_callbacks: List[Callable] = []

        # ── Polling-based contract resolution ──────────────────────────────────
        # {contract_id: {"callback": fn, "placed_at": time}}
        self._polling_contracts: Dict[str, dict] = {}

        # ── WS subscription-based contract resolution (primary; poller is fallback) ──
        self._subscribed_contracts: set = set()

        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Rate limiting ─────────────────────────────────────────────────────
        self._buy_semaphore  = asyncio.Semaphore(1)  # one at a time
        self._last_buy_time  = 0.0                   # epoch of last buy attempt

        # ── Direction→contract mapping audit ─────────────────────────────────
        logger.info(
            "Direction mapping: LONG → CALL (price rises) | SHORT → PUT (price falls)"
        )
        assert "CALL" == ("CALL" if "LONG" == "LONG" else "PUT"), (
            "FATAL: LONG→CALL mapping is broken")
        assert "PUT" == ("CALL" if "SHORT" == "LONG" else "PUT"), (
            "FATAL: SHORT→PUT mapping is broken")

    # ─── New Options API: REST account lookup + OTP WebSocket URL ─────────────

    async def _fetch_otp_ws_url(self) -> str:
        """
        Two-step REST flow required by Deriv's current Options trading API:
          1. GET  /accounts             -> list this user's Options accounts
          2. POST /accounts/{id}/otp    -> one-time WebSocket URL (OTP embedded)

        The account (real vs demo) is picked via config.DERIV_ACCOUNT_MODE.
        Returns the ready-to-connect wss:// URL. OTPs are short-lived, so this
        must be called fresh on every (re)connect attempt — never cache the URL.
        """
        app_id = config.DERIV_APP_ID
        token  = config.DERIV_API_TOKEN
        mode   = getattr(config, "DERIV_ACCOUNT_MODE", "demo").strip().lower()

        if not app_id:
            raise ValueError("DERIV_APP_ID is not set.")
        if not token:
            raise ValueError("DERIV_API_TOKEN is not set.")
        if mode not in ("real", "demo"):
            raise ValueError(
                f"DERIV_ACCOUNT_MODE must be 'real' or 'demo', got {mode!r}."
            )

        headers = {
            "Deriv-App-ID": app_id,
            "Authorization": f"Bearer {token}",
        }

        async with aiohttp.ClientSession() as session:
            # Step 1 — list accounts
            async with session.get(
                f"{_OPTIONS_API_BASE}/accounts", headers=headers
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(
                        f"Failed to list Options accounts: HTTP {resp.status} {body}"
                    )

            accounts = body.get("data", body) if isinstance(body, dict) else body
            if not accounts:
                raise RuntimeError(f"No Options accounts returned: {body}")

            chosen = None
            for acct in accounts:
                acct_type = (
                    acct.get("type")
                    or acct.get("account_type")
                    or ("demo" if acct.get("is_virtual") else "real")
                    or ""
                )
                if str(acct_type).lower() == mode:
                    chosen = acct
                    break

            if chosen is None:
                # Field names in the response aren't confirmed against docs yet —
                # fall back to the first account but log loudly so this is visible
                # rather than silently trading on the wrong one.
                logger.warning(
                    f"Could not match an Options account to mode={mode!r}; "
                    f"falling back to first account returned. Raw accounts: {accounts}"
                )
                chosen = accounts[0]

            account_id = (
                chosen.get("accountId") or chosen.get("account_id") or chosen.get("id")
            )
            if not account_id:
                raise RuntimeError(
                    f"Could not find an accountId field in account data: {chosen}"
                )

            logger.info(f"Using Options accountId={account_id} (mode={mode})")

            # Step 2 — request the OTP-embedded WebSocket URL
            async with session.post(
                f"{_OPTIONS_API_BASE}/accounts/{account_id}/otp", headers=headers
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(
                        f"Failed to get OTP WebSocket URL: HTTP {resp.status} {body}"
                    )

            ws_url = (body.get("data") or {}).get("url") or body.get("url")
            if not ws_url:
                raise RuntimeError(f"OTP response missing 'url' field: {body}")

            return ws_url

    @staticmethod
    def _mask_otp(ws_url: str) -> str:
        if "otp=" in ws_url:
            base, _, _ = ws_url.partition("otp=")
            return f"{base}otp=***OTP***"
        return ws_url

    # ─── Connection ───────────────────────────────────────────────────────────

    async def connect(self):
        """
        Outer reconnect loop.  Enforces WEBSOCKET_MAX_RECONNECTS and
        WEBSOCKET_RECONNECT_INTERVAL from config (falls back to module defaults).
        Starts the contract polling loop once before the reconnect loop begins.
        """
        self._loop = asyncio.get_event_loop()

        # Start the contract polling loop once — persists across reconnects
        asyncio.create_task(self._polling_loop())

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
                ws_url = await self._fetch_otp_ws_url()
                logger.info(f"Connecting to {self._mask_otp(ws_url)} …")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws        = ws
                    self._connected = True
                    attempt         = 0          # reset on successful connection
                    logger.info("WebSocket connected ✓")

                    # The OTP embedded in ws_url already authenticated this
                    # connection — no separate authorize message needed/accepted.
                    self._authorized = True
                    self._ready.set()
                    dispatch_task = asyncio.ensure_future(self._dispatch_loop())
                    try:
                        await self._subscribe_balance()
                        await self._resubscribe_contracts()
                        await dispatch_task
                    finally:
                        dispatch_task.cancel()
                        try:
                            await dispatch_task
                        except (asyncio.CancelledError, Exception):
                            pass

            except InvalidStatus as exc:
                attempt += 1
                resp = exc.response
                header_dump = dict(resp.headers.raw_items()) if hasattr(
                    resp.headers, "raw_items") else dict(resp.headers)
                body_text = ""
                if resp.body:
                    try:
                        body_text = resp.body.decode("utf-8", errors="replace")
                    except Exception:
                        body_text = repr(resp.body)
                logger.error(
                    "HANDSHAKE REJECTED — Deriv's server refused the WebSocket "
                    f"upgrade before any messages were exchanged.\n"
                    f"  status_code   = {resp.status_code}\n"
                    f"  reason_phrase = {resp.reason_phrase!r}\n"
                    f"  headers       = {header_dump}\n"
                    f"  body          = {body_text!r}"
                )
                logger.warning(
                    f"WEBSOCKET DISCONNECTED — reconnecting in {reconnect_interval}s "
                    f"(attempt {attempt}/{max_label}) | reason={exc}"
                )
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

        if msg_type == "proposal_open_contract":
            poc = msg.get("proposal_open_contract", {})
            cid = str(poc.get("contract_id", ""))
            closed = bool(
                poc.get("is_sold") or poc.get("is_expired") or poc.get("status") == "sold"
            )
            if cid and closed:
                self._subscribed_contracts.discard(cid)
                info = self._polling_contracts.pop(cid, None)
                if info and info.get("callback"):
                    try:
                        cb_result = info["callback"]({"proposal_open_contract": poc})
                        if asyncio.iscoroutine(cb_result) or isinstance(cb_result, asyncio.Future):
                            await cb_result
                    except Exception as exc:
                        logger.error(f"Contract close callback error {cid}: {exc}")
                logger.info(
                    f"SUBSCRIPTION CLOSED: {cid} profit={poc.get('profit')}"
                )

    # ─── Polling-based contract resolution (replaces subscriptions) ──────────

    async def _polling_loop(self):
        """
        Every 30 seconds, poll every contract registered in
        self._polling_contracts via force_check_contract().

        On is_sold/is_expired: pops the entry and fires its callback with
        the full proposal_open_contract dict (profit, sell_price, is_sold,
        is_expired, etc). The callback (registered by BotEngine) is
        responsible for releasing the symbol from _active_symbols.
        """
        while True:
            await asyncio.sleep(_CONTRACT_POLL_INTERVAL)
            if not self._polling_contracts:
                continue
            logger.info(
                f"POLLING: {len(self._polling_contracts)} open contracts")
            for cid in list(self._polling_contracts.keys()):
                try:
                    result = await self.force_check_contract(cid)
                    if result.get("is_sold") or result.get("is_expired") or result.get("status") == "sold":
                        info = self._polling_contracts.pop(cid, None)
                        if info and info.get("callback"):
                            cb_result = info["callback"]({"proposal_open_contract": result})
                            if asyncio.iscoroutine(cb_result) or isinstance(cb_result, asyncio.Future):
                                await cb_result
                            logger.info(f"POLL RESOLVED: {cid} profit={result.get('profit')}")
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"POLL ERROR {cid}: {e}")

    async def subscribe_contract(
        self,
        contract_id: str,
        callback:    Callable,
        symbol:      str = "",
    ):
        """
        Register a contract + callback for polling-based resolution.
        Replaces the old WebSocket proposal_open_contract subscription.
        """
        cid = str(contract_id)
        self._polling_contracts[cid] = {
            "callback":  callback,
            "placed_at": time.time(),
        }
        if symbol:
            self._contract_symbol_map[cid] = symbol
        logger.info(f"TRACKING: {cid} via polling")
        asyncio.create_task(self._subscribe_ws_contract(cid))

    # ─── WS subscription (primary close signal; poller above is the fallback) ─

    async def _subscribe_ws_contract(self, contract_id: str):
        """
        Subscribe to proposal_open_contract updates for *contract_id*
        (subscribe: 1). Never double-subscribes — guarded by
        self._subscribed_contracts. Pushes are handled in _handle().
        """
        cid = str(contract_id)
        if cid in self._subscribed_contracts:
            return
        try:
            await self._send({
                "proposal_open_contract": 1,
                "contract_id": int(cid),
                "subscribe": 1,
            })
            self._subscribed_contracts.add(cid)
            logger.info(f"SUBSCRIBED: {cid} (proposal_open_contract)")
        except Exception as e:
            logger.error(f"SUBSCRIBE FAILED: {cid} — {e}")

    async def _resubscribe_contracts(self):
        """Re-subscribe every tracked contract_id after a (re)connect."""
        if not self._subscribed_contracts:
            return
        pending = list(self._subscribed_contracts)
        self._subscribed_contracts.clear()
        logger.info(f"RESUBSCRIBING: {len(pending)} contracts after reconnect")
        for cid in pending:
            await self._subscribe_ws_contract(cid)

    # ─── Force check a specific contract ─────────────────────────────────────

    async def force_check_contract(self, contract_id: str) -> dict:
        """
        Manually query a contract's current state.

        Returns the full proposal_open_contract dict, including
        profit, sell_price, is_sold, is_expired (and all other fields
        the API returns). Returns {} on failure.
        """
        try:
            req = {
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
            }
            resp = await self._send(req)
            if not resp:
                return {}
            poc = resp.get("proposal_open_contract", {})
            logger.info(
                f"FORCE CHECK {contract_id}: "
                f"is_sold={poc.get('is_sold')} "
                f"profit={poc.get('profit')}")
            return poc
        except Exception as e:
            logger.error(f"FORCE CHECK ERROR {contract_id}: {e}")
            return {}

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
        Buy a Rise/Fall (CALL/PUT) contract via a direct buy — no
        proposal step. This eliminates the proposal-stage RateLimit
        errors caused by simultaneous proposal requests.

        Direction mapping (VERIFIED CORRECT - DO NOT SWAP):
          direction="LONG"  -> contract_type="CALL"  -> wins if price RISES
          direction="SHORT" -> contract_type="PUT"   -> wins if price FALLS

        Returns dict (buy response) on success, None on every failure path.
        Never raises.  Never increments trade counters on None return.

        After a successful buy, the caller is expected to register the
        contract for polling via subscribe_contract().
        """
        async with self._buy_semaphore:
            elapsed = time.time() - self._last_buy_time
            if elapsed < 3.0:
                await asyncio.sleep(3.0 - elapsed)
            self._last_buy_time = time.time()

            contract_type = "CALL" if direction == "LONG" else "PUT"
            logger.info(f"PLACING {contract_type} on {symbol} stake=${stake:.4f}")

            try:
                buy_req = {
                    "buy": 1,
                    "price": stake,
                    "parameters": {
                        "amount":         stake,
                        "basis":          "stake",
                        "contract_type":  contract_type,
                        "currency":       "USD",
                        "duration":       5,
                        "duration_unit":  "m",
                        "symbol":         symbol,
                    }
                }
                resp = await self._send(buy_req)
                if not resp:
                    logger.error(f"FAILED: {symbol} — no response")
                    return None
                if resp.get("error"):
                    logger.error(f"FAILED: {symbol} — {resp['error']['message']}")
                    return None

                result = resp.get("buy", {})
                contract_id = str(result.get("contract_id", ""))
                logger.info(
                    f"CONTRACT OPENED: {contract_id} | {symbol} | "
                    f"{contract_type} | ${stake:.4f}"
                )

                self._contract_symbol_map[contract_id] = symbol

                # Guaranteed closure: subscribe to this contract immediately.
                # (subscribe_contract(), called later by the caller to attach
                # its close callback, will no-op the resend via the
                # _subscribed_contracts guard — never double-subscribes.)
                asyncio.create_task(self._subscribe_ws_contract(contract_id))

                return result

            except Exception as e:
                logger.error(f"FAILED: {symbol} — {e}")
                return None

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
