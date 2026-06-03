import time
import logging
import httpx
import jwt

log = logging.getLogger(__name__)

# APNs JWT is valid for 1 hour; refresh after 50 minutes to stay safe.
_token_cache: dict[str, tuple[str, float]] = {}  # key_id -> (token, expires_at)


def _make_token(key_pem: str, key_id: str, team_id: str) -> tuple[str, float]:
    now = int(time.time())
    payload = {"iss": team_id, "iat": now}
    headers = {"alg": "ES256", "kid": key_id}
    token = jwt.encode(payload, key_pem, algorithm="ES256", headers=headers)
    return token, now + 3000


def _get_token(key_pem: str, key_id: str, team_id: str) -> str:
    cached = _token_cache.get(key_id)
    if cached and time.time() < cached[1]:
        return cached[0]
    token, expires = _make_token(key_pem, key_id, team_id)
    _token_cache[key_id] = (token, expires)
    return token


async def push(
    device_token: str,
    title: str,
    body: str,
    key_pem: str,
    key_id: str,
    team_id: str,
    bundle_id: str,
) -> bool:
    apns_jwt = _get_token(key_pem, key_id, team_id)
    url = f"https://api.push.apple.com/3/device/{device_token}"
    headers = {
        "authorization": f"bearer {apns_jwt}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "apns-priority": "5",
    }
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    try:
        async with httpx.AsyncClient(http2=True, timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                log.warning(f"APNs returned {resp.status_code}: {resp.text}")
            return resp.status_code == 200
    except Exception as e:
        log.error(f"APNs request failed: {e}")
        return False
