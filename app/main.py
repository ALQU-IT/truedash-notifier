import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
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
    # Short initial delay so the service is ready before the first check.
    await asyncio.sleep(10)
    while True:
        conf = cfg_module.load()
        interval = conf.poll_interval if conf else 300
        log.info("Running scheduled check")
        try:
            await notifier.check_and_notify()
        except Exception as e:
            log.error(f"Unexpected error during check: {e}")
        _last_check = datetime.now(timezone.utc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    _poll_task = asyncio.create_task(_poll_loop())
    yield
    if _poll_task:
        _poll_task.cancel()


app = FastAPI(title="TrueDash Notifier", version="1.0.0", lifespan=lifespan)


class RegisterRequest(BaseModel):
    device_token: str
    relay_url: str
    relay_token: str
    truenas_host: str
    truenas_port: int = 443
    truenas_api_key: str
    verify_tls: bool = False
    poll_interval: int = 900


@app.post("/api/register", status_code=200)
async def register(req: RegisterRequest):
    conf = cfg_module.Config(**req.model_dump())
    cfg_module.save(conf)
    log.info(f"Device registered: ...{req.device_token[-6:]}")
    # Kick off an immediate check in the background.
    asyncio.create_task(notifier.check_and_notify())
    return {"status": "registered"}


@app.get("/api/status")
async def status():
    conf = cfg_module.load()
    return {
        "registered": conf is not None,
        "last_check": _last_check.isoformat() if _last_check else None,
        "version": "1.0.0",
    }


@app.delete("/api/unregister", status_code=200)
async def unregister():
    cfg_module.delete()
    log.info("Device unregistered")
    return {"status": "unregistered"}


@app.get("/health")
async def health():
    return {"ok": True}
