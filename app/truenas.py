import json
import logging
import httpx

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 10_000_000


async def _get(
    host: str, port: int, api_key: str, path: str,
    params: dict | None = None, verify: bool = True,
):
    url = f"https://{host}:{port}/api/v2.0{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(verify=verify, timeout=15) as client:
        async with client.stream("GET", url, headers=headers, params=params) as resp:
            resp.raise_for_status()
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("Response too large")
            return json.loads(body)


async def get_pools(host: str, port: int, api_key: str, verify: bool = True) -> list:
    result = await _get(host, port, api_key, "/pool", verify=verify)
    return result if isinstance(result, list) else []


async def get_apps(host: str, port: int, api_key: str, verify: bool = True) -> list:
    result = await _get(host, port, api_key, "/app", verify=verify)
    return result if isinstance(result, list) else []


async def get_dataset(
    host: str, port: int, api_key: str, pool_name: str, verify: bool = True
) -> dict | None:
    result = await _get(
        host, port, api_key, "/pool/dataset",
        params={"id": pool_name, "extra.retrieve_children": "false"},
        verify=verify,
    )
    if isinstance(result, list) and result:
        return result[0]
    return None
