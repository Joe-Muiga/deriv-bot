"""
symbol_audit.py — One-time diagnostic (NOT part of the live bot loop).

Connects to Deriv, pulls every symbol in active_symbols on this account,
calls contracts_for on each one, groups results by the market/submarket
Deriv itself reports (not guessed prefixes), prints a table, saves the
full result to symbol_contract_map.json, and flags two known edge cases:

  1. Range Break / DEX symbols that are expected to lack Digits,
     Accumulators, and Rise/Fall contract types.
  2. Whether any Bear/Bull Market symbols are present in the live list
     at all (Deriv has added/removed this category before).

── AUTH PATTERN — READ BEFORE RUNNING ──────────────────────────────────────
This script deliberately does NOT reuse deriv_client.py's connect() /
_fetch_otp_ws_url() path. That path calls a REST "Options trading API"
at https://api.derivws.com/trading/v1/options/... which does not appear
in Deriv's public/current API docs or the official deriv-api client
(github.com/deriv-com/deriv-api) — every current reference still uses
the single WebSocket endpoint below with a plain `authorize` message.
That REST flow is very likely the actual cause of your June 2026
connection problems, not (only) the token/app_id format change.

Instead this script uses the plain, documented pattern — the same one
already implemented, but never called, in deriv_client.py's own
_authorize() method:

    wss://ws.derivws.com/websockets/v3?app_id=<DERIV_APP_ID>
    → send {"authorize": "<DERIV_API_TOKEN>"}

If this script authorizes successfully and the OTP-based bot doesn't,
that's strong evidence the OTP flow, not your token, is the problem.
─────────────────────────────────────────────────────────────────────────

Usage:
    python symbol_audit.py
"""

import asyncio
import json
import logging
from collections import defaultdict

import websockets

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("symbol_audit")

REQUEST_DELAY = 0.3   # seconds between contracts_for calls — stay under Deriv's rate limits
REQUEST_TIMEOUT = 20  # seconds


def _ws_url() -> str:
    app_id = getattr(config, "DERIV_APP_ID", None)
    if not app_id:
        raise ValueError("DERIV_APP_ID is not set in config.")
    return f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"


def classify_contract_types(contract_types):
    """Map raw Deriv contract_type codes to human-readable buckets."""
    buckets = set()
    for ct in contract_types:
        ct_up = (ct or "").upper()
        if ct_up in ("CALL", "PUT"):
            buckets.add("Rise/Fall")
        elif ct_up in ("CALLE", "PUTE"):
            buckets.add("Higher/Lower")
        elif ct_up.startswith("DIGIT"):
            buckets.add("Digits")
        elif ct_up == "ACCU":
            buckets.add("Accumulators")
        elif ct_up.startswith("MULT"):
            buckets.add("Multipliers")
        elif ct_up in ("ONETOUCH", "NOTOUCH"):
            buckets.add("Touch/No Touch")
        elif ct_up:
            buckets.add(ct_up)  # unrecognised/new contract type — surface it raw, don't hide it
    return buckets


class AuditClient:
    """Minimal, single-purpose WS client. Not a copy of DerivClient —
    intentionally small so this script has no hidden dependency on the
    file being audited."""

    def __init__(self):
        self._ws = None
        self._req_id = 1

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect_and_authorize(self) -> dict:
        token = getattr(config, "DERIV_API_TOKEN", None)
        if not token:
            raise ValueError("DERIV_API_TOKEN is not set in config.")

        url = _ws_url()
        logger.info(f"Connecting to {url.split('app_id=')[0]}app_id=*** ...")
        self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=20)

        auth_req = {"authorize": token, "req_id": self._next_id()}
        await self._ws.send(json.dumps(auth_req))
        raw = await asyncio.wait_for(self._ws.recv(), timeout=REQUEST_TIMEOUT)
        msg = json.loads(raw)

        if msg.get("error"):
            raise RuntimeError(
                f"Auth failed: {msg['error'].get('message')} (code={msg['error'].get('code')})"
            )

        account = msg.get("authorize", {})
        logger.info(
            f"Authorized \u2713 | Account: {account.get('loginid')} | "
            f"Balance: {account.get('balance')} {account.get('currency', '')}"
        )
        return account

    async def send(self, payload: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        payload = dict(payload)
        payload["req_id"] = self._next_id()
        await self._ws.send(json.dumps(payload))
        # Deriv may interleave other messages (e.g. late pushes); loop until
        # we see the matching req_id rather than assuming the next frame is ours.
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("req_id") == payload["req_id"]:
                return msg

    async def close(self):
        if self._ws is not None:
            await self._ws.close()


async def get_active_symbols(client: AuditClient) -> list:
    resp = await client.send({"active_symbols": "brief", "product_type": "basic"}, timeout=REQUEST_TIMEOUT)
    if resp.get("error"):
        raise RuntimeError(f"active_symbols failed: {resp['error']}")
    return resp.get("active_symbols", [])


async def get_contracts_for(client: AuditClient, symbol: str):
    """Returns (contract_types: list[str] | None, error: dict | None)."""
    resp = await client.send(
        {"contracts_for": symbol, "currency": "USD", "product_type": "basic"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.get("error"):
        return None, resp["error"]
    cf = resp.get("contracts_for", {})
    available = cf.get("available", [])
    contract_types = sorted({c.get("contract_type") for c in available if c.get("contract_type")})
    return contract_types, None


async def main():
    client = AuditClient()
    result_map = {}
    by_submarket = defaultdict(list)

    try:
        await client.connect_and_authorize()

        logger.info("Fetching active_symbols ...")
        symbols = await get_active_symbols(client)
        logger.info(f"{len(symbols)} active symbols returned.")

        for i, s in enumerate(symbols):
            symbol = s.get("symbol")
            market = s.get("market_display_name") or s.get("market") or "Unknown"
            submarket = s.get("submarket_display_name") or s.get("submarket") or "Unknown"
            display_name = s.get("display_name", symbol)
            exchange_open = bool(s.get("exchange_is_open", 0))

            logger.info(f"[{i + 1}/{len(symbols)}] contracts_for {symbol} ({display_name}) ...")
            contract_types, error = await get_contracts_for(client, symbol)

            entry = {
                "symbol": symbol,
                "display_name": display_name,
                "market": market,
                "submarket": submarket,
                "exchange_is_open": exchange_open,
                "contract_types": contract_types or [],
                "buckets": sorted(classify_contract_types(contract_types)) if contract_types else [],
                "error": error.get("message") if error else None,
            }
            result_map[symbol] = entry
            by_submarket[submarket].append(entry)

            if error:
                logger.warning(
                    f"  contracts_for FAILED for {symbol}: "
                    f"{error.get('message')} (code={error.get('code')})"
                )

            await asyncio.sleep(REQUEST_DELAY)

    finally:
        await client.close()

    # ── Table ────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print(f"{'Submarket':<28}{'Symbol':<14}{'Display Name':<26}{'Contract buckets'}")
    print("=" * 100)
    for submarket in sorted(by_submarket.keys()):
        for entry in sorted(by_submarket[submarket], key=lambda e: e["symbol"]):
            if entry["buckets"]:
                buckets_str = ", ".join(entry["buckets"])
            elif entry["error"]:
                buckets_str = f"ERROR: {entry['error']}"
            else:
                buckets_str = "NONE"
            print(f"{submarket:<28}{entry['symbol']:<14}{entry['display_name']:<26}{buckets_str}")
    print("=" * 100)

    # ── Save JSON ────────────────────────────────────────────────────────
    with open("symbol_contract_map.json", "w") as f:
        json.dump(
            {
                "symbol_count": len(result_map),
                "by_submarket": by_submarket,
                "symbols": result_map,
            },
            f,
            indent=2,
        )
    logger.info("Saved full result to symbol_contract_map.json")

    # ── Flags ────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("FLAGS")
    print("=" * 100)

    # Flag 1: Range Break / DEX — expected to lack Digits/Accumulators/Rise-Fall
    range_dex_found = False
    for submarket, entries in by_submarket.items():
        sm_lower = submarket.lower()
        if "range break" in sm_lower or "dex" in sm_lower:
            range_dex_found = True
            for e in entries:
                unexpected = set(e["buckets"]) & {"Digits", "Accumulators", "Rise/Fall"}
                if unexpected:
                    print(
                        f"  \u26a0 {e['symbol']} ({submarket}) unexpectedly HAS: "
                        f"{sorted(unexpected)} — verify this isn't a Deriv product change"
                    )
                else:
                    print(
                        f"  \u2713 {e['symbol']} ({submarket}) correctly lacks "
                        f"Digits/Accumulators/Rise-Fall — has: {e['buckets']}"
                    )
    if not range_dex_found:
        print("  (No Range Break / DEX submarket found in the live list — nothing to check.)")

    # Flag 2: Bear/Bull Market presence
    bear_bull_found = [
        e for entries in by_submarket.values() for e in entries
        if "bear" in e["submarket"].lower() or "bull" in e["submarket"].lower()
        or "bear" in e["market"].lower() or "bull" in e["market"].lower()
    ]
    if bear_bull_found:
        print(f"\n  \u2139 Bear/Bull Market symbols ARE present ({len(bear_bull_found)} found):")
        for e in bear_bull_found:
            print(f"      - {e['symbol']} ({e['display_name']})")
    else:
        print("\n  \u26a0 NO Bear/Bull Market symbols found in the current active_symbols list.")
        print("    Deriv has added/removed this category before — confirm this is expected")
        print("    rather than assuming your bot's symbol_manager has a bug.")

    print("=" * 100 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
