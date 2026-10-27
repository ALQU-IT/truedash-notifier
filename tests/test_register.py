"""/api/register: who may join and who may change the TrueNAS settings (H1)."""
import pytest

from conftest import REAL_KEY, register_body


def reg(client, body, bearer=None):
    headers = {"Authorization": f"Bearer {bearer or body['notifier_secret']}"}
    return client.post("/api/register", json=body, headers=headers)


def test_first_phone_registers(client, env):
    assert reg(client, register_body("a")).status_code == 200
    conf = env.config.load()
    assert [d.device_id for d in conf.devices] == ["dev-a"]
    assert conf.truenas_username == "truedash"


def test_second_phone_with_working_key_joins(client, env):
    reg(client, register_body("a"))
    assert reg(client, register_body("b")).status_code == 200
    assert len(env.config.load().devices) == 2
    # Its key was checked against the stored address.
    assert ("host.docker.internal", REAL_KEY, "truedash") in env.nas.logins


def test_stranger_cannot_take_over(client, env):
    reg(client, register_body("a"))
    resp = reg(client, register_body("evil", key="their-key", host="evil.example"))
    assert resp.status_code == 403
    conf = env.config.load()
    assert conf.truenas_host == "host.docker.internal"
    assert conf.truenas_api_key == REAL_KEY
    assert [d.device_id for d in conf.devices] == ["dev-a"]


def test_stranger_key_checked_at_stored_host_not_requested_one(client, env):
    reg(client, register_body("a"))
    # The attacker's own server would accept "their-key"; the check must not go there.
    env.nas.key = REAL_KEY
    reg(client, register_body("evil", key="their-key", host="evil.example"))
    assert all(host == "host.docker.internal" for host, _, _ in env.nas.logins)


def test_known_phone_may_change_settings(client, env):
    reg(client, register_body("a"))
    env.nas.logins.clear()
    body = register_body("a", n=2, host="nas.lan", key="new-key")
    assert reg(client, body, bearer="secret-a-1").status_code == 200
    conf = env.config.load()
    assert conf.truenas_host == "nas.lan"
    assert conf.truenas_api_key == "new-key"
    assert len(conf.devices) == 1
    # A known phone isn't put through the new-phone key check.
    assert ("host.docker.internal", "new-key", "truedash") not in env.nas.logins


def test_reinstalled_phone_recognised_by_device_id(client, env):
    reg(client, register_body("a"))
    # New notifier_secret (reinstall), same phone per the relay's device_id.
    body = register_body("a", n=2, secret="fresh-secret")
    assert reg(client, body).status_code == 200
    assert len(env.config.load().devices) == 1


def test_full_notifier_refuses_instead_of_evicting(client, env):
    for i in range(env.config.MAX_DEVICES):
        assert reg(client, register_body(f"p{i}")).status_code == 200
    assert reg(client, register_body("one-more")).status_code == 409
    assert len(env.config.load().devices) == env.config.MAX_DEVICES


def test_nas_unreachable_during_check_is_502(client, env):
    reg(client, register_body("a"))
    env.nas.reachable = False
    assert reg(client, register_body("b")).status_code == 502


def test_untrusted_relay_rejected(client):
    body = register_body("a")
    body["relay_url"] = "https://evil.example"
    assert reg(client, body).status_code == 400


def test_username_kept_when_omitted(client, env):
    reg(client, register_body("a"))
    body = register_body("a", n=2)
    del body["truenas_username"]
    reg(client, body, bearer="secret-a-1")
    assert env.config.load().truenas_username == "truedash"


def test_test_wake_sends_test_kind(client, env):
    reg(client, register_body("a"))
    env.wakes.clear()
    resp = client.post("/api/test", headers={"Authorization": "Bearer secret-a-1"})
    assert resp.status_code == 200
    assert env.wakes[-1]["kind"] == "test"
