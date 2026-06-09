import logging
import httpx

log = logging.getLogger(__name__)


async def wake(push_id: str, relay_url: str, push_secret: str) -> bool:
    """Sends a wake signal to the relay using the opaque push_id.
    The relay resolves the device token internally — it never passes through here."""
    url = relay_url.rstrip("/") + "/wake"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"push_id": push_id},
                headers={"Authorization": f"Bearer {push_secret}"},
            )
            if resp.status_code != 200:
                # Status code only — the response body could echo secrets.
                log.warning(f"Relay returned {resp.status_code}")
            return resp.status_code == 200
    except Exception as e:
        log.error(f"Relay request failed: {type(e).__name__}")
        return False
