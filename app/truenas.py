"""
TrueNAS middleware client over JSON-RPC 2.0 (wss://<host>:<port>/api/current).

Replaces the deprecated REST API (/api/v2.0), which is removed in TrueNAS
26.04. One socket is opened per poll cycle and authenticated once with
auth.login_with_api_key; every query for that cycle reuses it, so the
middleware records a single authentication per poll instead of one per request.
"""
import asyncio
import itertools
import json
import logging
import ssl

import websockets

log = logging.getLogger(__name__)

# Cap on a single JSON-RPC frame; oversized frames raise instead of buffering.
MAX_RESPONSE_BYTES = 10_000_000


class TrueNASError(Exception):
    pass


class Client:
    """One authenticated JSON-RPC socket. Use via `async with connect(...)`."""

    def __init__(self, host: str, port: int, api_key: str, verify: bool = True):
        self._uri = f"wss://{host}:{port}/api/current"
        self._api_key = api_key
        self._verify = verify
        self._ws = None
        self._ids = itertools.count(1)

    async def __aenter__(self) -> "Client":
        ssl_ctx = ssl.create_default_context()
        if not self._verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        self._ws = await websockets.connect(
            self._uri,
            ssl=ssl_ctx,
            max_size=MAX_RESPONSE_BYTES,
            open_timeout=15,
            close_timeout=5,
        )
        # Authenticate once; the socket carries the session for all later calls.
        ok = await self._call("auth.login_with_api_key", [self._api_key])
        if ok is not True:
            raise TrueNASError("auth.login_with_api_key rejected the API key")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _call(self, method: str, params: list):
        req_id = next(self._ids)
        await self._ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        ))
        # Read frames until the response matching our id arrives; ignore any
        # server-initiated notifications (which carry no matching id).
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            msg = json.loads(raw)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise TrueNASError(f"{method} failed: {_reason(msg['error'])}")
            return msg.get("result")

    async def pools(self) -> list:
        result = await self._call("pool.query", [])
        return result if isinstance(result, list) else []

    async def apps(self) -> list:
        result = await self._call("app.query", [])
        return result if isinstance(result, list) else []

    async def dataset(self, pool_name: str) -> dict | None:
        result = await self._call(
            "pool.dataset.query",
            [[["id", "=", pool_name]], {"extra": {"retrieve_children": False}}],
        )
        if isinstance(result, list) and result:
            return result[0]
        return None


def connect(host: str, port: int, api_key: str, verify: bool = True) -> Client:
    return Client(host, port, api_key, verify)


def _reason(error) -> str:
    """Pulls the human-readable text out of a JSON-RPC error object."""
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and data.get("reason"):
            return data["reason"]
        if error.get("message"):
            return error["message"]
    return "unknown error"
