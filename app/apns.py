import logging
import httpx

log = logging.getLogger(__name__)


async def wake(device_token: str, relay_url: str, relay_token: str) -> bool:
    """Calls the ALQU-IT relay which sends a silent APNs background push."""
    url = relay_url.rstrip("/") + "/wake"
    headers = {"Authorization": f"Bearer {relay_token}"}
    payload = {"device_token": device_token}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                log.warning(f"Relay returned {resp.status_code}: {resp.text}")
            return resp.status_code == 200
    except Exception as e:
        log.error(f"Relay request failed: {e}")
        return False
