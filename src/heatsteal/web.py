"""FastAPI app. Reads the latest status; puts commands on the control queue.

Never touches GPIO. ``loop`` is anything with ``status()``, ``submit(cmd)``
and ``recent_events(limit)``.
"""

from __future__ import annotations

import concurrent.futures
import hmac
import logging
from importlib import resources
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Config
from .model import Command

log = logging.getLogger(__name__)


class CommandBody(BaseModel):
    action: Literal["start", "stop", "reset", "pump"]
    loop: Literal["cold", "hot"] | None = None
    seconds: float | None = Field(default=None, gt=0)


def create_app(cfg: Config, loop) -> FastAPI:
    app = FastAPI(title="heatsteal", docs_url="/api/docs", openapi_url="/api/openapi.json")
    token = cfg.web.control_token

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not token:
            return
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(
                status_code=401, detail="missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return resources.files("heatsteal").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state() -> dict:
        return loop.status()

    @app.get("/api/ui")
    def ui() -> dict:
        return {"grafana_url": cfg.web.grafana_url.rstrip("/"), "auth_required": bool(token)}

    @app.get("/api/events")
    def events(limit: int = Query(default=50, ge=1, le=1000)) -> list[dict]:
        try:
            return loop.recent_events(limit)
        except Exception as e:
            log.warning("reading events failed: %s", e)
            raise HTTPException(status_code=503, detail=f"event log unavailable: {e}") from e

    @app.post("/api/command", dependencies=[Depends(require_token)])
    def command(body: CommandBody):
        if body.action == "pump" and (body.loop is None or body.seconds is None):
            return JSONResponse(
                status_code=409,
                content={"ok": False, "state": loop.status().get("state"), "reason": "pump needs loop and seconds"},
            )
        cmd = Command(body.action, body.loop, body.seconds)
        try:
            result = loop.submit(cmd)
        except concurrent.futures.TimeoutError:
            raise HTTPException(status_code=503, detail="controller did not answer in time") from None
        if not result.ok:
            return JSONResponse(
                status_code=409, content={"ok": False, "state": result.state.value, "reason": result.reason}
            )
        return {"ok": True, "state": result.state.value}

    return app
