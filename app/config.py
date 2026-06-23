import logging
import os
import tempfile
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/data/config.json"))


class Config(BaseModel):
    push_id: str           # opaque UUID returned by relay on registration
    push_secret: str       # per-device secret returned by relay on registration
    relay_url: str
    notifier_secret: str
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    verify_tls: bool = False
    poll_interval: int = Field(default=600, ge=60)

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
