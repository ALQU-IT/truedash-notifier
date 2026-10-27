import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import apns
import config as cfg_module
import truenas

log = logging.getLogger(__name__)

STATE_PATH = Path(os.getenv("STATE_PATH", "/data/state.json"))

# Serializes checks: the poll loop and register-triggered checks share state.
_check_lock = asyncio.Lock()

# Persisted across restarts (single uvicorn worker only — see docker-entrypoint.sh):
#   dedup            per-pool space/health thresholds, so we alert on transitions
#   app_updates      sorted identities of apps with an update pending
#   stale            push_ids whose credentials the relay no longer accepts
#                    (rotated, or the device token retired); that phone must
#                    re-enroll
#   wake_pending     push_ids whose last alert wake didn't reach the relay;
#                    retried on every later check until one succeeds
#   alerts           ids of TrueNAS's own ERROR-or-worse alerts already seen;
#                    None until the first successful check, which only records
#                    them, so an update doesn't push every standing alert
#   failing_since    ISO time of the first failed TrueNAS check in a row
#   unreachable_sent whether the phones were told about that failure
# Persisting means a routine container update no longer replays every standing
# alert, and the re-enroll signal is not lost on restart.
_persist: dict = {
    "dedup": {},
    "app_updates": [],
    "stale": [],
    "wake_pending": [],
    "alerts": None,
    "failing_since": None,
    "unreachable_sent": False,
}

# Types of the persisted keys, for loading (None-able keys can't use the default's type).
_PERSIST_TYPES = {
    "dedup": dict,
    "app_updates": list,
    "stale": list,
    "wake_pending": list,
    "alerts": list,
    "failing_since": str,
    "unreachable_sent": bool,
}

# Tell the phones once when TrueNAS has been unreachable this long (seconds).
UNREACHABLE_AFTER = int(os.getenv("UNREACHABLE_AFTER", "3600"))

# TrueNAS alert levels that wake the phone (its own alerts: SMART, scrub,
# replication, ...). INFO, NOTICE and WARNING don't.
ALERT_LEVELS = {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}

# Pool states from best to worst; a move to a worse one alerts. Anything not
# listed counts as bad (2).
_POOL_SEVERITY = {"ONLINE": 0, "DEGRADED": 1, "FAULTED": 2, "OFFLINE": 2, "UNAVAIL": 3, "REMOVED": 3}

# Volatile diagnostics (reset on restart, surfaced via /api/status).
_last_check: Optional[datetime] = None
_last_check_ok: Optional[bool] = None
_last_error: Optional[str] = None


def load_state() -> None:
    """Loads persisted dedup state from disk on startup. Corruption is
    non-fatal — we start clean rather than crash-loop."""
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception as e:
        log.warning(f"State file exists but could not be loaded: {e}")
        return
    if isinstance(data, dict):
        for key in _persist:
            if key in data and isinstance(data[key], _PERSIST_TYPES[key]):
                _persist[key] = data[key]
        # Before 1.2.0 the notifier served one phone and kept a single flag.
        if data.get("credentials_stale") is True:
            conf = cfg_module.load()
            _persist["stale"] = [d.push_id for d in conf.devices] if conf else []


def _save_state() -> None:
    """Atomic write: temp file then os.replace, so a crash can't corrupt state."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=STATE_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(_persist, f)
            os.replace(tmp_path, STATE_PATH)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except Exception as e:
        # State is a best-effort cache; a write failure must not break checks.
        log.warning(f"Could not persist state: {e}")


def reset_state() -> None:
    _persist["dedup"] = {}
    _persist["app_updates"] = []
    _persist["stale"] = []
    _persist["wake_pending"] = []
    _persist["alerts"] = None
    _persist["failing_since"] = None
    _persist["unreachable_sent"] = False
    STATE_PATH.unlink(missing_ok=True)


def _set_flag(key: str, push_id: str, on: bool) -> None:
    ids = _persist[key]
    if on and push_id not in ids:
        ids.append(push_id)
    elif not on and push_id in ids:
        ids.remove(push_id)
    else:
        return
    _save_state()


def credentials_stale(push_id: str) -> bool:
    return push_id in _persist["stale"]


def mark_credentials_stale(push_id: str) -> None:
    _set_flag("stale", push_id, True)


def clear_credentials_stale(push_id: str) -> None:
    _set_flag("stale", push_id, False)


def wake_pending(push_id: str) -> bool:
    return push_id in _persist["wake_pending"]


def mark_wake_pending(push_id: str) -> None:
    _set_flag("wake_pending", push_id, True)


def forget_device(push_id: str) -> None:
    """Drops a phone's flags once it is no longer registered here."""
    for key in ("stale", "wake_pending"):
        _set_flag(key, push_id, False)


def status_info() -> dict:
    """Diagnostics for /api/status."""
    return {
        "last_check": _last_check.isoformat() if _last_check else None,
        "last_check_ok": _last_check_ok,
        "last_error": _last_error,
    }


async def check_and_notify() -> None:
    async with _check_lock:
        await _check_and_notify()


async def _check_and_notify() -> None:
    global _last_check, _last_check_ok, _last_error

    conf = cfg_module.load()
    if conf is None:
        log.debug("No config — skipping check")
        return

    _last_check = datetime.now(timezone.utc)
    try:
        # One socket, authenticated once, reused for every query this cycle.
        async with truenas.connect(
            conf.truenas_host, conf.truenas_port, conf.truenas_api_key, conf.verify_tls,
            conf.truenas_username,
        ) as tn:
            pools_raw = await tn.pools()
            apps_raw = await tn.apps()
            try:
                alerts_raw = await tn.alerts()
            except Exception:
                # Alerts are additive: losing them must not fail the check.
                alerts_raw = None

            # Enrich pools with logical dataset space.
            for pool in pools_raw:
                try:
                    ds = await tn.dataset(pool["name"])
                    if ds:
                        pool["_used"] = ds["used"]["parsed"]
                        pool["_avail"] = ds["available"]["parsed"]
                except Exception:
                    pass
    except Exception as e:
        _last_check_ok = False
        _last_error = f"{type(e).__name__}: {e}"
        log.warning(f"TrueNAS fetch failed: {_last_error}")
        await _note_unreachable(conf)
        return

    _last_check_ok = True
    _last_error = None
    _persist["failing_since"] = None
    _persist["unreachable_sent"] = False

    space_triggered = _check_pool_space(pools_raw)
    health_triggered = _check_pool_health(pools_raw)
    updates_triggered = _check_app_updates(apps_raw)
    alerts_triggered = _check_alerts(alerts_raw) if alerts_raw is not None else False
    # Persist the updated thresholds regardless of whether we wake.
    _save_state()

    # A wake that failed earlier is retried: its alert is already recorded in
    # dedup, so no later transition would ever trigger it again. The app reads
    # the server's current state when the push lands, so a late wake is fine.
    # While credentials are stale a retry can only fail; re-enrollment runs a
    # check straight away, which sends it.
    triggered = space_triggered or health_triggered or updates_triggered or alerts_triggered
    await _wake_devices(conf, triggered)


async def _wake_devices(conf, triggered: bool, kind: Optional[str] = None) -> None:
    """Wakes every phone when `triggered`, and retries earlier failed wakes."""
    for device in conf.devices:
        pid = device.push_id
        retry = wake_pending(pid) and not credentials_stale(pid)
        if not (triggered or retry):
            continue
        result = await apns.wake(pid, conf.relay_url, device.push_secret, kind if triggered else None)
        _set_flag("wake_pending", pid, result != apns.OK)
        if result == apns.OK:
            log.info(f"Wake sent to relay (push_id ...{pid[-6:]})")
        elif result == apns.UNAUTHORIZED:
            # Credentials rotated by the relay — flag for re-enrollment.
            mark_credentials_stale(pid)
            log.warning(f"Wake unauthorized (push_id ...{pid[-6:]}) — awaiting re-enrollment from app")
        elif result == apns.RATE_LIMITED:
            log.warning(f"Wake skipped (push_id ...{pid[-6:]}) — relay rate limit reached")
        else:
            log.warning(f"Wake delivery failed (push_id ...{pid[-6:]})")


async def _note_unreachable(conf) -> None:
    """After UNREACHABLE_AFTER seconds of failed checks in a row, wakes the
    phones once so they learn alerts can't be checked (API key revoked or
    expired, TrueNAS address changed). Reset by the next good check."""
    now = datetime.now(timezone.utc)
    since = _persist.get("failing_since")
    if since is None:
        _persist["failing_since"] = now.isoformat()
        _save_state()
        return
    try:
        started = datetime.fromisoformat(since)
    except ValueError:
        _persist["failing_since"] = now.isoformat()
        _save_state()
        return
    if _persist.get("unreachable_sent") or (now - started).total_seconds() < UNREACHABLE_AFTER:
        return
    _persist["unreachable_sent"] = True
    _save_state()
    log.warning("TrueNAS unreachable for a while — telling the phones")
    await _wake_devices(conf, True, apns.KIND_UNREACHABLE)


def _check_alerts(alerts: list) -> bool:
    """Wakes for a new TrueNAS alert of level ERROR or worse. The first check
    only records the standing ones."""
    current = sorted({
        str(a.get("uuid") or a.get("id") or a.get("key") or a.get("formatted"))
        for a in alerts
        if isinstance(a, dict)
        and not a.get("dismissed")
        and str(a.get("level", "")).upper() in ALERT_LEVELS
        and (a.get("uuid") or a.get("id") or a.get("key") or a.get("formatted"))
    })
    previous = _persist.get("alerts")
    _persist["alerts"] = current
    if previous is None:
        return False
    seen = set(previous)
    return any(a not in seen for a in current)


def _check_pool_space(pools: list) -> bool:
    dedup = _persist["dedup"]
    triggered = False
    for pool in pools:
        pid = pool.get("id") or pool.get("name")
        used = pool.get("_used")
        avail = pool.get("_avail")
        if used is None or avail is None:
            continue
        total = used + avail
        if total == 0:
            continue
        free_pct = avail / total * 100
        key = f"pool_space_{pid}"
        was_alerting = dedup.get(key, False)
        # Hysteresis band 20–25% avoids flapping around the threshold.
        if free_pct < 20 and not was_alerting:
            dedup[key] = True
            triggered = True
        elif free_pct >= 25 and was_alerting:
            dedup[key] = False
    return triggered


def _check_pool_health(pools: list) -> bool:
    dedup = _persist["dedup"]
    triggered = False
    for pool in pools:
        pid = pool.get("id") or pool.get("name")
        status = pool.get("status", "ONLINE").upper()
        key = f"pool_health_{pid}"
        last = dedup.get(key, "ONLINE")
        # Any move to a worse state alerts: ONLINE → DEGRADED, and also
        # DEGRADED → FAULTED when a second disk goes.
        if _POOL_SEVERITY.get(status, 2) > _POOL_SEVERITY.get(str(last).upper(), 2):
            triggered = True
        dedup[key] = status
    return triggered


def _check_app_updates(apps: list) -> bool:
    """Identity-based: wake when a NEW app becomes updatable. Counting instead
    would miss churn (one update applied while another appears keeps the count
    flat), so we track the set of updatable app identities."""
    current = sorted(
        str(a.get("id") or a.get("name"))
        for a in apps
        if (a.get("upgrade_available") or a.get("update_available"))
        and (a.get("id") or a.get("name"))
    )
    previous = set(_persist.get("app_updates", []))
    triggered = any(name not in previous for name in current)
    _persist["app_updates"] = current
    return triggered
