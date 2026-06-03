import asyncio
import logging

import apns
import config as cfg_module
import truenas

log = logging.getLogger(__name__)

# In-memory state for notification deduplication (mirrors iOS NotificationManager logic).
_state: dict = {}


async def check_and_notify() -> None:
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

    # Enrich pools with logical dataset space (same as iOS app).
    for pool in pools_raw:
        try:
            ds = await truenas.get_dataset(
                conf.truenas_host, conf.truenas_port, conf.truenas_api_key,
                pool["name"], conf.verify_tls
            )
            if ds:
                pool["_used"] = ds["used"]["parsed"]
                pool["_avail"] = ds["available"]["parsed"]
        except Exception:
            pass

    await _check_pool_space(pools_raw, conf)
    await _check_pool_health(pools_raw, conf)
    await _check_app_updates(apps_raw, conf)


async def _push(title: str, body: str, conf: cfg_module.Config) -> None:
    ok = await apns.push(
        conf.device_token, title, body,
        conf.apns_key_pem, conf.apns_key_id, conf.apns_team_id, conf.bundle_id,
    )
    if ok:
        log.info(f"Push sent: {title}")
    else:
        log.warning(f"Push failed: {title}")


async def _check_pool_space(pools: list, conf: cfg_module.Config) -> None:
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
        free_gb = avail / 1_073_741_824
        key = f"pool_space_{pid}"
        was_alerting = _state.get(key, False)
        if free_pct < 20 and not was_alerting:
            await _push(
                "Low Pool Space",
                f"Pool \"{pool['name']}\" has {free_pct:.0f}% free ({free_gb:.1f} GB)",
                conf,
            )
            _state[key] = True
        elif free_pct >= 25 and was_alerting:
            _state[key] = False


async def _check_pool_health(pools: list, conf: cfg_module.Config) -> None:
    for pool in pools:
        pid = pool.get("id", pool.get("name"))
        status = pool.get("status", "ONLINE").upper()
        key = f"pool_health_{pid}"
        last = _state.get(key, "ONLINE")
        if status != "ONLINE" and last == "ONLINE":
            await _push(
                "Pool Health Alert",
                f"Pool \"{pool['name']}\" is now {status.capitalize()}",
                conf,
            )
        _state[key] = status


async def _check_app_updates(apps: list, conf: cfg_module.Config) -> None:
    updatable = sum(
        1 for a in apps if a.get("upgrade_available") or a.get("update_available")
    )
    last = _state.get("app_updates", 0)
    if updatable > 0 and updatable > last:
        if updatable == 1:
            title, body = "App Update Available", "1 app has an update available"
        else:
            title = f"{updatable} App Updates Available"
            body = f"{updatable} apps have updates available"
        await _push(title, body, conf)
    _state["app_updates"] = updatable
