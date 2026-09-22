import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

import apns as notifier_apns
import certgen
import config as cfg_module
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

_poll_task:  Optional[asyncio.Task] = None
_cert_fingerprint: Optional[str] = None


async def _poll_loop() -> None:
    await asyncio.sleep(10)
    while True:
        conf = cfg_module.load()
        interval = conf.poll_interval if conf else 600
        log.info("Running scheduled check")
        try:
            await notifier.check_and_notify()
        except Exception as e:
            log.error(f"Unexpected error during check: {e}")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task, _cert_fingerprint

    # Restore persisted dedup state so a restart doesn't replay standing alerts.
    notifier.load_state()

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


app = FastAPI(title="TrueDash Notifier", version="1.1.1", lifespan=lifespan)

# The only relay we trust. The enrollment path skips notifier_secret auth and
# relies on the relay vouching for the token, so the relay must NOT come from
# the request body — otherwise anyone on the LAN could point verification at
# their own server and take over the notifier's config.
TRUSTED_RELAY_URL = os.getenv("RELAY_URL", "https://truedash-relay.alqu.ch").rstrip("/")

# Strong refs to fire-and-forget tasks so they aren't garbage-collected mid-run.
_background_tasks: set = set()

# Minimum seconds between /api/test wakes.
TEST_COOLDOWN = 30
_last_test: Optional[datetime] = None


def _require_auth(authorization: Optional[str], expected_secret: str) -> None:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token or not hmac.compare_digest(token, expected_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _verify_enrollment(relay_url: str, token: str) -> tuple[str, str]:
    """Calls the relay to verify and consume a single-use enrollment token.
    The relay vouches for the device and returns its push_id + push_secret,
    so those credentials never traverse the app→notifier link."""
    url = relay_url.rstrip("/") + "/enrollment/verify"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"enrollment_token": token})
    except Exception as e:
        log.error(f"Enrollment verify failed: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Could not reach relay to verify enrollment token")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired enrollment token")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Relay returned a malformed response")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Relay returned a malformed response")
    push_id = data.get("push_id")
    push_secret = data.get("push_secret")
    if not push_id or not push_secret:
        raise HTTPException(status_code=502, detail="Relay did not return push_id and push_secret")
    return push_id, push_secret


class RegisterRequest(BaseModel):
    relay_url: str
    notifier_secret: str
    truenas_host: str
    truenas_api_key: str
    truenas_port: int = 443
    verify_tls: bool = False
    # Notifier-owned setting: when omitted on re-registration the existing
    # value is preserved rather than reset, so the app need not resend it.
    poll_interval: Optional[int] = Field(default=None, ge=60)
    # Either supply an enrollment_token (relay returns push_id + push_secret),
    # or supply push_id + push_secret directly (legacy path).
    enrollment_token: Optional[str] = None
    push_id: Optional[str] = None
    push_secret: Optional[str] = None

    @model_validator(mode="after")
    def _credentials_present(self) -> "RegisterRequest":
        if not self.enrollment_token and not (self.push_id and self.push_secret):
            raise ValueError("either enrollment_token or push_id+push_secret is required")
        return self


@app.post("/api/register", status_code=200)
async def register(
    req: RegisterRequest,
    authorization: Optional[str] = Header(default=None),
):
    existing = cfg_module.load()

    # Validate before redeeming the single-use token, so a bad request can't
    # burn it.
    if req.relay_url.rstrip("/") != TRUSTED_RELAY_URL:
        raise HTTPException(status_code=400, detail="Untrusted relay_url")

    # Auth model depends on how the device proves itself:
    #  - enrollment_token: the relay vouches for the device by verifying the
    #    single-use token, so that IS the proof of authenticity. Re-enrollment
    #    must stay idempotent — an app reinstall carries a fresh notifier_secret,
    #    so gating on the old stored one would wrongly 401 (TD-N7). Redeeming
    #    the token below is the gate instead.
    #  - legacy push_id/push_secret: no relay round-trip, so fall back to the
    #    stored notifier_secret to authenticate the caller.
    if req.enrollment_token:
        # Redeem the single-use token exactly once; on failure the app must
        # request a fresh enrollment rather than retrying this dead token.
        push_id, push_secret = await _verify_enrollment(TRUSTED_RELAY_URL, req.enrollment_token)
    else:
        expected = existing.notifier_secret if existing else req.notifier_secret
        _require_auth(authorization, expected)
        push_id, push_secret = req.push_id, req.push_secret

    # Preserve a previously customized poll_interval when the app omits it.
    if req.poll_interval is not None:
        poll_interval = req.poll_interval
    elif existing is not None:
        poll_interval = existing.poll_interval
    else:
        poll_interval = 600

    conf = cfg_module.Config(
        push_id=push_id,
        push_secret=push_secret,
        relay_url=TRUSTED_RELAY_URL,
        notifier_secret=req.notifier_secret,
        truenas_host=req.truenas_host,
        truenas_port=req.truenas_port,
        truenas_api_key=req.truenas_api_key,
        verify_tls=req.verify_tls,
        poll_interval=poll_interval,
    )
    cfg_module.save(conf)
    # Fresh credentials stored — clear any stale-credential flag from a prior
    # rotation so the poll loop and /api/status reflect the healed state.
    notifier.clear_credentials_stale()
    log.info(f"Device registered with push_id: ...{push_id[-6:]}")
    task = asyncio.create_task(notifier.check_and_notify())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "registered"}


@app.get("/api/status")
async def status(authorization: Optional[str] = Header(default=None)):
    conf = cfg_module.load()
    if conf is None:
        raise HTTPException(status_code=404, detail="Not registered")
    _require_auth(authorization, conf.notifier_secret)
    return {
        "registered": True,
        # True after the relay rotated our credentials (app reinstall): the app
        # should re-enroll by POSTing /api/register with a fresh enrollment_token.
        "credentials_stale": notifier.credentials_stale(),
        # Diagnostics: when the last poll ran, whether it reached TrueNAS, and
        # the last error text — so "why did notifications stop?" is answerable
        # from the app without reading container logs.
        **notifier.status_info(),
        "version": "1.1.1",
    }


@app.delete("/api/unregister", status_code=200)
async def unregister(authorization: Optional[str] = Header(default=None)):
    conf = cfg_module.load()
    if conf is None:
        raise HTTPException(status_code=404, detail="Not registered")
    _require_auth(authorization, conf.notifier_secret)
    cfg_module.delete()
    # Drop persisted alert state too, so a later registration starts clean.
    notifier.reset_state()
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
    result = await notifier_apns.wake(conf.push_id, conf.relay_url, conf.push_secret)
    if result == notifier_apns.UNAUTHORIZED:
        notifier.mark_credentials_stale()
        raise HTTPException(
            status_code=401,
            detail="Credentials rotated — re-enroll with a fresh enrollment_token",
        )
    if result != notifier_apns.OK:
        raise HTTPException(status_code=502, detail="Relay wake failed")
    log.info("Test wake sent to relay")
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"ok": True, "cert_fingerprint": _cert_fingerprint}
