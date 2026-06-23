import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import apns as notifier_apns
import certgen
import config as cfg_module
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

_last_check: Optional[datetime] = None
_poll_task:  Optional[asyncio.Task] = None
_cert_fingerprint: Optional[str] = None


async def _poll_loop() -> None:
    global _last_check
    await asyncio.sleep(10)
    while True:
        conf = cfg_module.load()
        interval = conf.poll_interval if conf else 600
        log.info("Running scheduled check")
        try:
            await notifier.check_and_notify()
            _last_check = datetime.now(timezone.utc)
        except Exception as e:
            log.error(f"Unexpected error during check: {e}")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task, _cert_fingerprint

    # Generate TLS cert if not present and compute fingerprint for /health.
    cert_path, _ = certgen.ensure_cert()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        raw = cert.fingerprint(hashes.SHA256())
        _cert_fingerprint = ":".join(f"{b:02X}" for b in raw)
        log.info(f"TLS cert fingerprint (SHA-256): {_cert_fingerprint}")
    except Exception as e:
        log.warning(f"Could not compute cert fingerprint: {e}")

    _poll_task = asyncio.create_task(_poll_loop())
    yield
    if _poll_task:
        _poll_task.cancel()


app = FastAPI(title="TrueDash Notifier", version="1.0.0", lifespan=lifespan)

# Minimum seconds between /api/test wakes.
TEST_COOLDOWN = 30
_last_test: Optional[datetime] = None


def _require_auth(authorization: Optional[str], expected_secret: str) -> None:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token or not hmac.compare_digest(token, expected_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _validate_enrollment_token(relay_url: str, token: str) -> None:
    """Calls the relay to validate and consume a single-use enrollment token."""
    url = relay_url.rstrip("/") + "/validate-enrollment"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"enrollment_token": token})
        if resp.status_code != 200 or not resp.json().get("valid"):
            raise HTTPException(status_code=401, detail="Invalid or expired enrollment token")
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Enrollment token validation failed: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Could not reach relay to validate enrollment token")


class RegisterRequest(BaseModel):
    push_id: str
    push_secret: str
    relay_url: str
    notifier_secret: str
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    verify_tls: bool = False
    poll_interval: int = 600
    enrollment_token: Optional[str] = None  # single-use relay-minted token


@app.post("/api/register", status_code=200)
async def register(
    req: RegisterRequest,
    authorization: Optional[str] = Header(default=None),
):
    existing = cfg_module.load()
    expected = existing.notifier_secret if existing else req.notifier_secret
    _require_auth(authorization, expected)

    # If the relay minted an enrollment token, validate and consume it.
    if req.enrollment_token:
        await _validate_enrollment_token(req.relay_url, req.enrollment_token)

    conf = cfg_module.Config(**{k: v for k, v in req.model_dump().items()
                                if k != "enrollment_token"})
    cfg_module.save(conf)
    log.info(f"Device registered with push_id: ...{req.push_id[-6:]}")
    asyncio.create_task(notifier.check_and_notify())
    return {"status": "registered"}


@app.get("/api/status")
async def status(authorization: Optional[str] = Header(default=None)):
    conf = cfg_module.load()
    if conf is None:
        raise HTTPException(status_code=404, detail="Not registered")
    _require_auth(authorization, conf.notifier_secret)
    return {
        "registered": True,
        "last_check": _last_check.isoformat() if _last_check else None,
        "version": "1.0.0",
    }


@app.delete("/api/unregister", status_code=200)
async def unregister(authorization: Optional[str] = Header(default=None)):
    conf = cfg_module.load()
    if conf is None:
        raise HTTPException(status_code=404, detail="Not registered")
    _require_auth(authorization, conf.notifier_secret)
    cfg_module.delete()
    log.info("Device unregistered")
    return {"status": "unregistered"}


@app.post("/api/test", status_code=200)
async def test_wake(authorization: Optional[str] = Header(default=None)):
    global _last_test
    conf = cfg_module.load()
    if conf is None:
        raise HTTPException(status_code=404, detail="Not registered")
    _require_auth(authorization, conf.notifier_secret)
    now = datetime.now(timezone.utc)
    if _last_test and (now - _last_test).total_seconds() < TEST_COOLDOWN:
        raise HTTPException(status_code=429, detail="Test cooldown active")
    _last_test = now
    ok = await notifier_apns.wake(conf.push_id, conf.relay_url, conf.push_secret)
    if not ok:
        raise HTTPException(status_code=502, detail="Relay wake failed")
    log.info("Test wake sent to relay")
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"ok": True, "cert_fingerprint": _cert_fingerprint}
