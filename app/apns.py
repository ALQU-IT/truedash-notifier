import logging
import httpx

log = logging.getLogger(__name__)

# Wake outcomes. UNAUTHORIZED means the relay rotated our credentials
# (app reinstall) and the notifier must re-enroll; ERROR is any other failure.
OK = "ok"
UNAUTHORIZED = "unauthorized"
ERROR = "error"


async def wake(push_id: str, relay_url: str, push_secret: str) -> str:
    """Sends a wake signal to the relay using the opaque push_id.
    The relay resolves the device token internally — it never passes through here.
    Returns one of OK / UNAUTHORIZED / ERROR."""
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
    # Status code only — the response body could echo secrets.
    log.warning(f"Relay returned {resp.status_code}")
    return ERROR
