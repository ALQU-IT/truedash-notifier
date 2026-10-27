import hmac
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/data/config.json"))


# Phones beyond this are dropped oldest-first, so a stream of new enrollments
# can't grow the config without bound.
MAX_DEVICES = 10


class Device(BaseModel):
    """One phone this notifier wakes."""
    push_id: str           # opaque UUID returned by relay on registration
    push_secret: str       # per-device secret returned by relay on registration
    notifier_secret: str   # the phone's own secret for this notifier's API
    # Stable relay-issued id for the phone (same across re-enrollments), so a
    # returning phone replaces its entry. None for phones enrolled through an
    # older relay or the legacy push_id/push_secret path.
    device_id: Optional[str] = None


class Config(BaseModel):
    devices: list[Device] = Field(default_factory=list)
    relay_url: str
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    # The API key's username, for auth.login_ex (sent by TrueDash 6.0.7+).
    truenas_username: Optional[str] = None
    verify_tls: bool = False
    poll_interval: int = Field(default=600, ge=60)

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_device(cls, data):
        """Configs written before 1.2.0 held one phone's credentials at the top level."""
        if isinstance(data, dict) and "devices" not in data and data.get("push_id"):
            data = dict(data)
            data["devices"] = [{
                "push_id": data.pop("push_id"),
                "push_secret": data.pop("push_secret", ""),
                "notifier_secret": data.pop("notifier_secret", ""),
            }]
        return data

    def device_for_secret(self, secret: str) -> Optional[Device]:
        """The phone whose notifier_secret this is (constant-time compare)."""
        found = None
        for d in self.devices:
            if secret and hmac.compare_digest(d.notifier_secret, secret):
                found = d
        return found

    @field_validator("relay_url")
    @classmethod
    def relay_url_must_be_https(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("relay_url must use https://")
        return v


def load() -> Optional[Config]:
    if not CONFIG_PATH.exists():
        return None
    try:
        return Config.model_validate_json(CONFIG_PATH.read_text())
    except Exception as e:
        log.warning(f"Config file exists but could not be loaded: {e}")
        return None


def save(cfg: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file created 0o600, then renamed over the target,
    # so secrets are never world-readable and a crash can't corrupt the config.
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(cfg.model_dump_json(indent=2))
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        os.unlink(tmp_path)
        raise


def delete() -> None:
    CONFIG_PATH.unlink(missing_ok=True)
