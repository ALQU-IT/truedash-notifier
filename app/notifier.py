import asyncio
import logging

import apns
import config as cfg_module
import truenas

log = logging.getLogger(__name__)

# In-memory state for deduplication — mirrors iOS NotificationManager thresholds.
_state: dict = {}

# Serializes checks: the poll loop and register-triggered checks share _state.
_check_lock = asyncio.Lock()


async def check_and_notify() -> None:
    async with _check_lock:
        await _check_and_notify()


async def _check_and_notify() -> None:
    conf = cfg_module.load()
    if conf is None:
        log.debug("No config — skipping check")
        return

    try:
        pools_raw = await truenas.get_pools(
            conf.truenas_host, conf.truenas_port, conf.truenas_api_key, conf.verify_tls
        )
        apps_raw = await truenas.get_apps(
            conf.truenas_host, conf.truenas_port, conf.truenas_api_key, conf.verify_tls
        )
    except Exception as e:
        log.warning(f"TrueNAS fetch failed: {e}")
        return

    # Enrich pools with logical dataset space.
    for pool in pools_raw:
        try:
            ds = await truenas.get_dataset(
                conf.truenas_host, conf.truenas_port, conf.truenas_api_key,
                pool["name"], conf.verify_tls,
            )
            if ds:
                pool["_used"] = ds["used"]["parsed"]
                pool["_avail"] = ds["available"]["parsed"]
        except Exception:
            pass

    space_triggered = _check_pool_space(pools_raw)
    health_triggered = _check_pool_health(pools_raw)
    updates_triggered = _check_app_updates(apps_raw)
    triggered = space_triggered or health_triggered or updates_triggered

    if triggered:
        ok = await apns.wake(conf.push_id, conf.relay_url, conf.push_secret)
        if ok:
            log.info("Wake sent to relay")
        else:
            log.warning("Wake delivery failed")


def _check_pool_space(pools: list) -> bool:
    triggered = False
    for pool in pools:
        pid = pool.get("id", pool.get("name"))
        used = pool.get("_used")
        avail = pool.get("_avail")
        if used is None or avail is None:
            continue
        total = used + avail
        if total == 0:
            continue
        free_pct = avail / total * 100
        key = f"pool_space_{pid}"
        was_alerting = _state.get(key, False)
        if free_pct < 20 and not was_alerting:
            _state[key] = True
            triggered = True
        elif free_pct >= 25 and was_alerting:
            _state[key] = False
    return triggered


def _check_pool_health(pools: list) -> bool:
    triggered = False
    for pool in pools:
        pid = pool.get("id", pool.get("name"))
        status = pool.get("status", "ONLINE").upper()
        key = f"pool_health_{pid}"
        last = _state.get(key, "ONLINE")
        if status != "ONLINE" and last == "ONLINE":
            triggered = True
        _state[key] = status
    return triggered


def _check_app_updates(apps: list) -> bool:
    updatable = sum(
        1 for a in apps if a.get("upgrade_available") or a.get("update_available")
    )
    last = _state.get("app_updates", 0)
    triggered = updatable > 0 and updatable > last
    _state["app_updates"] = updatable
    return triggered
