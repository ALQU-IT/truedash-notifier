"""The checks that decide when phones are woken."""
import asyncio
from datetime import datetime, timedelta, timezone

from conftest import register_body


def setup(client, env):
    client.post("/api/register", json=register_body("a"),
                headers={"Authorization": "Bearer secret-a-1"})
    check(env)  # first check records the standing state
    env.wakes.clear()


def check(env):
    asyncio.run(env.notifier.check_and_notify())


def test_quiet_when_nothing_changes(client, env):
    setup(client, env)
    check(env)
    assert env.wakes == []


def test_pool_space_below_20_percent(client, env):
    setup(client, env)
    env.nas.datasets["tank"] = (850, 150)
    check(env)
    assert len(env.wakes) == 1
    check(env)
    assert len(env.wakes) == 1  # no repeat while still low


def test_health_escalation_alerts_again(client, env):
    setup(client, env)
    env.nas.pools[0]["status"] = "DEGRADED"
    check(env)
    assert len(env.wakes) == 1
    env.nas.pools[0]["status"] = "FAULTED"
    check(env)
    assert len(env.wakes) == 2
    env.nas.pools[0]["status"] = "DEGRADED"  # better: no alert
    check(env)
    assert len(env.wakes) == 2


def test_new_app_update(client, env):
    setup(client, env)
    env.nas.apps = [{"name": "plex", "upgrade_available": True}]
    check(env)
    assert len(env.wakes) == 1


def test_truenas_alerts_error_or_worse(client, env):
    env.nas.alerts = [{"uuid": "old", "level": "CRITICAL", "dismissed": False}]
    setup(client, env)
    # The standing alert from before didn't wake anyone.
    assert env.wakes == []
    env.nas.alerts.append({"uuid": "w", "level": "WARNING", "dismissed": False})
    check(env)
    assert env.wakes == []
    env.nas.alerts.append({"uuid": "e", "level": "ERROR", "dismissed": False})
    check(env)
    assert len(env.wakes) == 1
    assert env.wakes[0]["kind"] is None


def test_first_check_records_alerts_without_waking(client, env):
    env.nas.alerts = [{"uuid": "x", "level": "CRITICAL"}]
    client.post("/api/register", json=register_body("a"),
                headers={"Authorization": "Bearer secret-a-1"})
    env.wakes.clear()
    env.notifier._persist["alerts"] = None
    check(env)
    assert env.wakes == []


def test_unreachable_told_once_after_an_hour(client, env):
    setup(client, env)
    env.nas.reachable = False
    check(env)
    assert env.wakes == []
    # Pretend the failures started more than an hour ago.
    past = datetime.now(timezone.utc) - timedelta(seconds=env.notifier.UNREACHABLE_AFTER + 60)
    env.notifier._persist["failing_since"] = past.isoformat()
    check(env)
    assert env.wakes == [{"push_id": "pid-a:1", "kind": "unreachable"}]
    check(env)
    assert len(env.wakes) == 1
    # Back again: the next outage is reported again.
    env.nas.reachable = True
    check(env)
    assert env.notifier._persist["failing_since"] is None
    assert env.notifier._persist["unreachable_sent"] is False


def test_state_survives_restart(client, env, tmp_path):
    setup(client, env)
    env.nas.pools[0]["status"] = "DEGRADED"
    check(env)
    env.notifier._persist["dedup"] = {}
    env.notifier.load_state()
    assert env.notifier._persist["dedup"]["pool_health_1"] == "DEGRADED"
