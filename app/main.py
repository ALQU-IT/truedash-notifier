import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import config as cfg_module
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

_last_check: Optional[datetime] = None
_poll_task: Optional[asyncio.Task] = None


async def _poll_loop() -> None:
    global _last_check
    await asyncio.sleep(10)
    while True:
        conf = cfg_module.load()
        interval = conf.poll_interval if conf else 600
        log.info("Running scheduled check")
        try:
            await notifier.check_and_notify()
            _last_check = datetime.now(timezone.utc)
        except Exception as e:
            log.error(f"Unexpected error during check: {e}")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    _poll_task = asyncio.create_task(_poll_loop())
    yield
    if _poll_task:
        _poll_task.cancel()


app = FastAPI(title="TrueDash Notifier", version="1.0.0", lifespan=lifespan)


def _require_auth(authorization: Optional[str], conf: cfg_module.Config) -> None:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if token != conf.notifier_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


class RegisterRequest(BaseModel):
    push_id: str
    relay_secret: str
    relay_url: str
    notifier_secret: str
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    verify_tls: bool = True
    poll_interval: int = 600


@app.post("/api/register", status_code=200)
async def register(
    req: RegisterRequest,
    authorization: Optional[str] = Header(default=None),
):
    existing = cfg_module.load()
    if existing is not None:
        _require_auth(authorization, existing)

    conf = cfg_module.Config(**req.model_dump())
    cfg_module.save(conf)
    log.info(f"Device registered with push_id: ...{req.push_id[-6:]}")
    asyncio.create_task(notifier.check_and_notify())
    return {"status": "registered"}


@app.get("/api/status")
async def status(authorization: Optional[str] = Header(default=None)):
    conf = cfg_module.load()
    if conf is None:
        return {"registered": False, "last_check": None, "version": "1.0.0"}
    _require_auth(authorization, conf)
    return {
        "registered": True,
        "last_check": _last_check.isoformat() if _last_check else None,
        "version": "1.0.0",
    }


@app.delete("/api/unregister", status_code=200)
async def unregister(authorization: Optional[str] = Header(default=None)):
    conf = cfg_module.load()
    if conf is None:
        raise HTTPException(status_code=404, detail="Not registered")
    _require_auth(authorization, conf)
    cfg_module.delete()
    log.info("Device unregistered")
    return {"status": "unregistered"}


@app.get("/health")
async def health():
    return {"ok": True}
