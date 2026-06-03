import json
import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/data/config.json"))


class Config(BaseModel):
    device_token: str
    apns_key_id: str
    apns_team_id: str
    apns_key_pem: str
    bundle_id: str = "ALQU-IT.TrueDash"
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    verify_tls: bool = False
    poll_interval: int = 900  # seconds, default 15 min


def load() -> Optional[Config]:
    if not CONFIG_PATH.exists():
        return None
    try:
        return Config.model_validate_json(CONFIG_PATH.read_text())
    except Exception:
        return None


def save(cfg: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2))


def delete() -> None:
    CONFIG_PATH.unlink(missing_ok=True)
