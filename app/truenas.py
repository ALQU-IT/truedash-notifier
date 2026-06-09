import logging
import httpx

log = logging.getLogger(__name__)


async def _get(
    host: str, port: int, api_key: str, path: str,
    params: dict | None = None, verify: bool = True,
):
    url = f"https://{host}:{port}/api/v2.0{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(verify=verify, timeout=15) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        if len(resp.content) > 10_000_000:
            raise ValueError("Response too large")
        return resp.json()


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
