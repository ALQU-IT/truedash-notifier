import logging
import httpx

log = logging.getLogger(__name__)

# Wake outcomes. UNAUTHORIZED means the relay no longer accepts our credentials
# (rotated by an app reinstall, or dropped after APNs retired the device token)
# and the notifier must re-enroll; RATE_LIMITED means the relay's per-device
# budget is spent for now; ERROR is any other failure.
OK = "ok"
UNAUTHORIZED = "unauthorized"
RATE_LIMITED = "rate_limited"
ERROR = "error"


async def wake(push_id: str, relay_url: str, push_secret: str) -> str:
    """Sends a wake signal to the relay using the opaque push_id.
    The relay resolves the device token internally — it never passes through here.
    Returns one of OK / UNAUTHORIZED / RATE_LIMITED / ERROR."""
    url = relay_url.rstrip("/") + "/wake"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"push_id": push_id},
                headers={"Authorization": f"Bearer {push_secret}"},
            )
    except Exception as e:
        log.error(f"Relay request failed: {type(e).__name__}")
        return ERROR

    if resp.status_code == 200:
        return OK
    if resp.status_code == 401:
        # Stale/rotated credentials — signal re-enrollment, don't treat as fatal.
        log.warning("Relay returned 401 — credentials rotated, re-enrollment needed")
        return UNAUTHORIZED
    if resp.status_code == 410:
        # APNs retired the device token, so the relay deleted this registration.
        # Only a fresh enrollment (which carries the app's current token) heals it.
        log.warning("Relay returned 410 — device token retired, re-enrollment needed")
        return UNAUTHORIZED
    if resp.status_code == 429:
        log.warning("Relay returned 429 — wake rate limit reached")
        return RATE_LIMITED
    # Status code only — the response body could echo secrets.
    log.warning(f"Relay returned {resp.status_code}")
    return ERROR
