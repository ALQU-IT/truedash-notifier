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
import truenas

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


VERSION = "1.3.0"

app = FastAPI(title="TrueDash Notifier", version=VERSION, lifespan=lifespan)

# The only relay we trust. The enrollment path skips notifier_secret auth and
# relies on the relay vouching for the token, so the relay must NOT come from
# the request body — otherwise anyone on the LAN could point verification at
# their own server and take over the notifier's config.
TRUSTED_RELAY_URL = os.getenv("RELAY_URL", "https://truedash-relay.alqu.ch").rstrip("/")

# Strong refs to fire-and-forget tasks so they aren't garbage-collected mid-run.
_background_tasks: set = set()

# Minimum seconds between /api/test wakes, per phone (keyed by push_id).
TEST_COOLDOWN = 30
_last_test: dict[str, datetime] = {}


def _require_auth(authorization: Optional[str], expected_secret: str) -> None:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token or not hmac.compare_digest(token, expected_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _verify_enrollment(relay_url: str, token: str) -> tuple[str, str, Optional[str]]:
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

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid or expired enrollment token")
    if resp.status_code != 200:
        # Relay overloaded or down: the token may still be good, so don't tell
        # the app it was rejected.
        log.warning(f"Enrollment verify: relay returned {resp.status_code}")
        raise HTTPException(status_code=502, detail="Relay could not verify the enrollment token")

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
    # Relays before 2.3.0 don't send it.
    device_id = data.get("device_id")
    return push_id, push_secret, device_id if isinstance(device_id, str) and device_id else None


async def _require_working_key(existing: cfg_module.Config, req: "RegisterRequest") -> None:
    """The new phone's API key must log in to the TrueNAS this notifier already
    watches (the stored address, not the one in the request, which the caller
    controls)."""
    username = req.truenas_username or existing.truenas_username
    try:
        async with truenas.connect(existing.truenas_host, existing.truenas_port,
                                   req.truenas_api_key, existing.verify_tls, username):
            pass
    except truenas.TrueNASError:
        raise HTTPException(status_code=403, detail="API key not accepted by TrueNAS")
    except Exception as e:
        log.warning(f"Could not check the API key with TrueNAS: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Could not reach TrueNAS to check the API key")


class RegisterRequest(BaseModel):
    relay_url: str
    notifier_secret: str
    truenas_host: str
    truenas_api_key: str
    # The API key's username, for auth.login_ex (TrueDash 6.0.7+). When omitted
    # on re-registration the stored one is kept.
    truenas_username: Optional[str] = None
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


def _bearer(authorization: Optional[str]) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return ""


def _require_device(authorization: Optional[str]) -> tuple[cfg_module.Config, cfg_module.Device]:
    """The config and the phone this request's Bearer secret belongs to."""
    conf = cfg_module.load()
    if conf is None or not conf.devices:
        raise HTTPException(status_code=404, detail="Not registered")
    device = conf.device_for_secret(_bearer(authorization))
    if device is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return conf, device


@app.post("/api/register", status_code=200)
async def register(
    req: RegisterRequest,
    authorization: Optional[str] = Header(default=None),
):
    existing = cfg_module.load()
    devices = list(existing.devices) if existing else []

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
    #  - legacy push_id/push_secret: no relay round-trip, so fall back to a
    #    stored notifier_secret to authenticate the caller.
    #
    # Several phones can share one notifier (1.2.0). The phone being
    # (re-)registered replaces only its own entry: matched by the relay's
    # device_id, else by its notifier_secret.
    device_id = None
    # A phone this notifier already serves may change the TrueNAS settings. A
    # new phone joining others must prove it holds a working TrueNAS API key:
    # anyone can get an enrollment token from the relay (its registration key
    # ships in the app), so the token alone doesn't show the caller owns this
    # NAS (review H1, 1.3.0).
    known = bool(existing and (existing.device_for_secret(req.notifier_secret)
                               or existing.device_for_secret(_bearer(authorization))))
    if req.enrollment_token:
        # Redeem the single-use token exactly once; on failure the app must
        # request a fresh enrollment rather than retrying this dead token.
        push_id, push_secret, device_id = await _verify_enrollment(TRUSTED_RELAY_URL, req.enrollment_token)
        if device_id and existing and any(d.device_id == device_id for d in existing.devices):
            known = True
        if device_id:
            # Entries without a device_id predate it (one phone per notifier,
            # or an older relay), so the first phone with one takes them over,
            # as before 1.2.0. Any other phone re-enrolls on its next connect.
            same = lambda d: d.device_id in (device_id, None)
        elif existing and existing.device_for_secret(req.notifier_secret):
            same = lambda d: hmac.compare_digest(d.notifier_secret, req.notifier_secret)
        else:
            # An older relay can't say whether this is a new phone or a
            # reinstalled one, so keep the pre-1.2.0 rule: it takes over.
            same = lambda d: True
    else:
        if existing and existing.devices:
            if existing.device_for_secret(_bearer(authorization)) is None:
                raise HTTPException(status_code=401, detail="Unauthorized")
        else:
            _require_auth(authorization, req.notifier_secret)
        push_id, push_secret = req.push_id, req.push_secret
        bearer = _bearer(authorization)
        same = lambda d: hmac.compare_digest(d.notifier_secret, bearer)

    if existing and existing.devices and not known:
        await _require_working_key(existing, req)

    replaced = [d for d in devices if same(d)]
    devices = [d for d in devices if not same(d)]
    if len(devices) >= cfg_module.MAX_DEVICES:
        # Full: refuse rather than push out a phone that still uses it.
        raise HTTPException(status_code=409, detail="Too many devices registered")
    devices.append(cfg_module.Device(
        push_id=push_id,
        push_secret=push_secret,
        notifier_secret=req.notifier_secret,
        device_id=device_id,
    ))

    # Preserve a previously customized poll_interval when the app omits it.
    if req.poll_interval is not None:
        poll_interval = req.poll_interval
    elif existing is not None:
        poll_interval = existing.poll_interval
    else:
        poll_interval = 600

    username = req.truenas_username
    if username is None and existing is not None:
        username = existing.truenas_username

    conf = cfg_module.Config(
        devices=devices,
        relay_url=TRUSTED_RELAY_URL,
        truenas_host=req.truenas_host,
        truenas_port=req.truenas_port,
        truenas_api_key=req.truenas_api_key,
        truenas_username=username,
        verify_tls=req.verify_tls,
        poll_interval=poll_interval,
    )
    cfg_module.save(conf)
    # A wake that failed for this phone's old credentials carries over, so the
    # check below delivers it with the fresh ones.
    if any(notifier.wake_pending(d.push_id) for d in replaced):
        notifier.mark_wake_pending(push_id)
    for d in replaced:
        notifier.forget_device(d.push_id)
    log.info(f"Device registered with push_id: ...{push_id[-6:]} ({len(devices)} device(s))")
    task = asyncio.create_task(notifier.check_and_notify())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "registered", "devices": len(devices)}


@app.get("/api/status")
async def status(authorization: Optional[str] = Header(default=None)):
    conf, device = _require_device(authorization)
    return {
        "registered": True,
        # True after the relay stopped accepting this phone's credentials (app
        # reinstall, token retired): the app should re-enroll by POSTing
        # /api/register with a fresh enrollment_token.
        "credentials_stale": notifier.credentials_stale(device.push_id),
        # How many phones this notifier wakes (1.2.0+).
        "devices": len(conf.devices),
        # Diagnostics: when the last poll ran, whether it reached TrueNAS, and
        # the last error text — so "why did notifications stop?" is answerable
        # from the app without reading container logs.
        **notifier.status_info(),
        "version": VERSION,
    }


@app.delete("/api/unregister", status_code=200)
async def unregister(authorization: Optional[str] = Header(default=None)):
    conf, device = _require_device(authorization)
    remaining = [d for d in conf.devices if d.push_id != device.push_id]
    notifier.forget_device(device.push_id)
    if remaining:
        cfg_module.save(conf.model_copy(update={"devices": remaining}))
        log.info(f"Device unregistered, {len(remaining)} device(s) remain")
    else:
        cfg_module.delete()
        # Drop persisted alert state too, so a later registration starts clean.
        notifier.reset_state()
        log.info("Last device unregistered")
    # The app deletes the notifier app only when no other phone still uses it.
    return {"status": "unregistered", "remaining_devices": len(remaining)}


@app.post("/api/test", status_code=200)
async def test_wake(authorization: Optional[str] = Header(default=None)):
    conf, device = _require_device(authorization)
    now = datetime.now(timezone.utc)
    last = _last_test.get(device.push_id)
    if last and (now - last).total_seconds() < TEST_COOLDOWN:
        raise HTTPException(status_code=429, detail="Test cooldown active")
    _last_test[device.push_id] = now
    result = await notifier_apns.wake(device.push_id, conf.relay_url, device.push_secret,
                                      notifier_apns.KIND_TEST)
    if result == notifier_apns.UNAUTHORIZED:
        notifier.mark_credentials_stale(device.push_id)
        raise HTTPException(
            status_code=401,
            detail="Credentials rotated — re-enroll with a fresh enrollment_token",
        )
    if result == notifier_apns.RATE_LIMITED:
        raise HTTPException(status_code=429, detail="Relay wake limit reached, try again later")
    if result != notifier_apns.OK:
        raise HTTPException(status_code=502, detail="Relay wake failed")
    log.info("Test wake sent to relay")
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"ok": True, "cert_fingerprint": _cert_fingerprint}
