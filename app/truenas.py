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
import socket
import ssl

import websockets

log = logging.getLogger(__name__)

# Cap on a single JSON-RPC frame; oversized frames raise instead of buffering.
MAX_RESPONSE_BYTES = 10_000_000

# The TrueNAS host as seen from inside the container (the compose maps it to
# the Docker host gateway). Traffic to it never leaves the machine, and the
# certificate can't name it, so TLS is not verified for it.
LOCAL_HOST = "host.docker.internal"


class TrueNASError(Exception):
    pass


class Client:
    """One authenticated JSON-RPC socket. Use via `async with connect(...)`."""

    def __init__(self, host: str, port: int, api_key: str, verify: bool = True,
                 username: str | None = None):
        self._host = host
        self._port = port
        self._uri = f"wss://{host}:{port}/api/current"
        self._api_key = api_key
        self._username = (username or "").strip()
        self._verify = verify and host != LOCAL_HOST
        self._ws = None
        self._ids = itertools.count(1)

    async def __aenter__(self) -> "Client":
        ssl_ctx = ssl.create_default_context()
        if not self._verify:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        uri = self._uri
        if self._host == LOCAL_HOST:
            uri = f"wss://{await _local_host_address()}:{self._port}/api/current"
        self._ws = await websockets.connect(
            uri,
            ssl=ssl_ctx,
            max_size=MAX_RESPONSE_BYTES,
            open_timeout=15,
            close_timeout=5,
        )
        # Authenticate once; the socket carries the session for all later calls.
        await self._login()
        return self

    async def _login(self) -> None:
        """auth.login_ex when the key's username is known (TrueNAS 25.04+; the
        old method is gone in 27). The old auth.login_with_api_key only when the
        server doesn't know login_ex, never after a refusal."""
        if self._username:
            try:
                result = await self._call("auth.login_ex", [{
                    "mechanism": "API_KEY_PLAIN",
                    "username": self._username,
                    "api_key": self._api_key,
                }])
            except TrueNASError as e:
                if not _is_unknown_method(str(e)):
                    raise
            else:
                if isinstance(result, dict) and str(result.get("response_type", "")).upper() == "SUCCESS":
                    return
                raise TrueNASError("auth.login_ex rejected the API key")
        ok = await self._call("auth.login_with_api_key", [self._api_key])
        if ok is not True:
            raise TrueNASError("auth.login_with_api_key rejected the API key")

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
                error = msg["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise TrueNASError(f"{method} failed: {_reason(error)}" + (f" ({code})" if code else ""))
            return msg.get("result")

    async def pools(self) -> list:
        result = await self._call("pool.query", [])
        return result if isinstance(result, list) else []

    async def apps(self) -> list:
        result = await self._call("app.query", [])
        return result if isinstance(result, list) else []

    async def alerts(self) -> list:
        result = await self._call("alert.list", [])
        return result if isinstance(result, list) else []

    async def dataset(self, pool_name: str) -> dict | None:
        result = await self._call(
            "pool.dataset.query",
            [[["id", "=", pool_name]], {"extra": {"retrieve_children": False}}],
        )
        if isinstance(result, list) and result:
            return result[0]
        return None


def connect(host: str, port: int, api_key: str, verify: bool = True,
            username: str | None = None) -> Client:
    return Client(host, port, api_key, verify, username)


async def _local_host_address() -> str:
    """host.docker.internal when the compose maps it (TrueDash 6.0.7+), else
    the container's default gateway, which is the Docker host too."""
    try:
        await asyncio.get_running_loop().getaddrinfo(LOCAL_HOST, None)
        return LOCAL_HOST
    except OSError:
        pass
    gateway = _default_gateway()
    if gateway is None:
        raise TrueNASError(f"{LOCAL_HOST} doesn't resolve and no default gateway was found")
    return gateway


def _default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """The IPv4 default gateway from the kernel's routing table."""
    try:
        with open(route_file) as f:
            next(f, None)
            for line in f:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
                    return socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little"))
    except OSError:
        return None
    return None


def _is_unknown_method(message: str) -> bool:
    m = message.lower()
    return ("-32601" in m or "unknown method" in m or "no such method" in m
            or "not a valid method" in m or "method not found" in m)


def _reason(error) -> str:
    """Pulls the human-readable text out of a JSON-RPC error object."""
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and data.get("reason"):
            return data["reason"]
        if error.get("message"):
            return error["message"]
    return "unknown error"
