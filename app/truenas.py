import logging
import httpx

log = logging.getLogger(__name__)


async def _get(host: str, port: int, api_key: str, path: str, verify: bool = False):
    url = f"https://{host}:{port}/api/v2.0{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(verify=verify, timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        if resp.headers.get("content-length", "1") == "0":
            return None
        data = resp.json()
        if isinstance(data, (list, dict)) and len(resp.content) > 10_000_000:
            raise ValueError("Response too large")
        return data


async def get_pools(host: str, port: int, api_key: str, verify: bool = False) -> list:
    result = await _get(host, port, api_key, "/pool", verify=verify)
    return result if isinstance(result, list) else []


async def get_apps(host: str, port: int, api_key: str, verify: bool = False) -> list:
    result = await _get(host, port, api_key, "/app", verify=verify)
    return result if isinstance(result, list) else []


async def get_dataset(
    host: str, port: int, api_key: str, pool_name: str, verify: bool = False
) -> dict | None:
    path = f"/pool/dataset?id={pool_name}&extra.retrieve_children=false"
    result = await _get(host, port, api_key, path, verify=verify)
    if isinstance(result, list) and result:
        return result[0]
    return None
