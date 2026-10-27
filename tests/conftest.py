import importlib
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

REAL_KEY = "real-api-key"


class FakeTrueNAS:
    """Stands in for truenas.connect(): one NAS whose state tests change."""

    def __init__(self, truenas_module):
        self.mod = truenas_module
        self.host = "host.docker.internal"
        self.key = REAL_KEY
        self.reachable = True
        self.pools = [{"id": 1, "name": "tank", "status": "ONLINE"}]
        self.datasets = {"tank": (100, 900)}  # used, available
        self.apps = []
        self.alerts = []
        self.logins = []

    def connect(self, host, port, api_key, verify=True, username=None):
        fake = self

        class Conn:
            async def __aenter__(self):
                fake.logins.append((host, api_key, username))
                if not fake.reachable or host != fake.host:
                    raise OSError("unreachable")
                if api_key != fake.key:
                    raise fake.mod.TrueNASError("auth.login_ex rejected the API key")
                return self

            async def __aexit__(self, *exc):
                return False

            async def pools(self):
                return [dict(p) for p in fake.pools]

            async def apps(self):
                return list(fake.apps)

            async def alerts(self):
                return list(fake.alerts)

            async def dataset(self, name):
                used, avail = fake.datasets[name]
                return {"used": {"parsed": used}, "available": {"parsed": avail}}

        return Conn()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    for name in ("main", "config", "notifier", "truenas", "apns", "certgen"):
        sys.modules.pop(name, None)
    main = importlib.import_module("main")
    nas = FakeTrueNAS(main.truenas)
    monkeypatch.setattr(main.truenas, "connect", nas.connect)

    wakes = []

    async def fake_wake(push_id, relay_url, push_secret, kind=None):
        wakes.append({"push_id": push_id, "kind": kind})
        return main.notifier_apns.OK

    monkeypatch.setattr(main.notifier_apns, "wake", fake_wake)

    # The relay: each enrollment token names the phone it was minted for.
    async def fake_verify(relay_url, token):
        phone = token.split(":")[0]
        return f"pid-{token}", f"psecret-{token}", f"dev-{phone}"

    monkeypatch.setattr(main, "_verify_enrollment", fake_verify)

    class Env:
        pass

    e = Env()
    e.main, e.nas, e.wakes = main, nas, wakes
    e.notifier, e.config = main.notifier, sys.modules["config"]
    return e


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    return TestClient(env.main.app)


def register_body(phone, n=1, key=REAL_KEY, host="host.docker.internal", secret=None):
    return {
        "relay_url": "https://truedash-relay.alqu.ch",
        "notifier_secret": secret or f"secret-{phone}-{n}",
        "truenas_host": host,
        "truenas_api_key": key,
        "truenas_username": "truedash",
        "enrollment_token": f"{phone}:{n}",
    }
