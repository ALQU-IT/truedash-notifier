import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/data/config.json"))


class Config(BaseModel):
    device_token: str
    relay_url: str
    relay_token: str
    notifier_secret: str
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    verify_tls: bool = True
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
    except Exception:
        return None


def save(cfg: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2))
    CONFIG_PATH.chmod(0o600)


def delete() -> None:
    CONFIG_PATH.unlink(missing_ok=True)
