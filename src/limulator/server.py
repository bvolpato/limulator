from __future__ import annotations

import hmac
import secrets
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .applications import AppError
from .profiles import list_profiles, public_metadata
from .service import DeviceError, DeviceService, device_metadata

MAX_IPA_BYTES = 512 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class InstallUploadLimit:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/install":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None:
            try:
                declared = int(length)
                if declared < 0:
                    raise ValueError
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return
            if declared > self.max_bytes:
                await JSONResponse({"detail": "Upload request is too large"}, status_code=413)(
                    scope, receive, send
                )
                return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Upload request is too large")
            return message

        await self.app(scope, bounded_receive, send)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    type: Literal["tap", "swipe", "text", "button"]
    x: float = 0
    y: float = 0
    x0: float = 0
    y0: float = 0
    x1: float = 0
    y1: float = 0
    duration_ms: float = 350
    text: str = Field(default="", max_length=4096)
    name: str = ""
    frame_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_action_fields(self):
        required = {
            "tap": {"x", "y"},
            "swipe": {"x0", "y0", "x1", "y1"},
            "text": {"text"},
            "button": {"name"},
        }[self.type]
        optional = {
            "tap": {"frame_id"},
            "swipe": {"duration_ms", "frame_id"},
            "text": set(),
            "button": set(),
        }[self.type]
        if not required <= self.model_fields_set:
            raise ValueError(f"{self.type} requires {', '.join(sorted(required))}")
        if self.model_fields_set - required - optional - {"type"}:
            raise ValueError(f"Unexpected fields for {self.type}")
        return self


class Launch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: str = Field(min_length=1, max_length=255)


class Fold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: str


def create_app(service: DeviceService, token: str | None = None, port: int = 8765) -> FastAPI:
    app = FastAPI(title="Limulator", docs_url=None, redoc_url=None, openapi_url=None)
    session_token = token or secrets.token_urlsafe(32)
    static = Path(__file__).parent / "web"
    app.state.token = session_token
    app.add_middleware(InstallUploadLimit, max_bytes=MAX_IPA_BYTES + MULTIPART_OVERHEAD_BYTES)

    def selected(device: str | None):
        if device is None:
            return service
        try:
            if hasattr(service, "for_device"):
                return service.for_device(device)
            registered = getattr(getattr(service, "runtime", None), "device", None)
            if registered is not None and device == registered.name:
                return service
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        raise HTTPException(status_code=404, detail=f"unknown device: {device}")

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            host = None
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "Only loopback hosts are allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }:
            return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            received = request.headers.get("x-limulator-token", "")
            if not hmac.compare_digest(received.encode("utf-8"), session_token.encode("utf-8")):
                return JSONResponse({"detail": "A session token is required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(DeviceError)
    @app.exception_handler(AppError)
    async def device_error(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        details = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in error.errors()]
        return JSONResponse({"detail": details}, status_code=422)

    @app.exception_handler(OSError)
    @app.exception_handler(RuntimeError)
    async def runtime_error(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (static / "index.html").read_text().replace("__LIMULATOR_TOKEN__", session_token)

    @app.get("/api/status")
    def status(device: str | None = None):
        return selected(device).status()

    @app.get("/api/devices")
    def devices():
        if hasattr(service, "devices"):
            return service.devices()
        registered = getattr(getattr(service, "runtime", None), "device", None)
        return {
            "default_device": registered.name if registered else None,
            "devices": [device_metadata(registered)] if registered else [],
            "profiles": [public_metadata(profile) for profile in list_profiles()],
        }

    @app.post("/api/start")
    def start(device: str | None = None):
        return selected(device).start()

    @app.post("/api/stop")
    def stop(device: str | None = None):
        return selected(device).stop()

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/api/screen.png")
    def screen(device: str | None = None):
        frame = selected(device).capture()
        return Response(
            frame.png,
            media_type="image/png",
            headers={
                "X-Limulator-Frame": frame.identity,
                "Content-Disposition": 'inline; filename="limulator.png"',
            },
        )

    @app.get("/api/apps")
    def apps(device: str | None = None):
        return {"apps": selected(device).apps()}

    @app.post("/api/input")
    def input_event(event: Input, device: str | None = None):
        target = selected(device)
        if event.type == "tap":
            return target.tap(event.x, event.y, event.frame_id)
        if event.type == "swipe":
            return target.swipe(event.x0, event.y0, event.x1, event.y1, event.duration_ms, event.frame_id)
        if event.type == "text":
            return target.text(event.text)
        if event.type == "button":
            return target.button(event.name)
        raise HTTPException(status_code=422, detail="Unknown input type")

    @app.post("/api/fold")
    def fold(event: Fold, device: str | None = None):
        return selected(device).fold(event.position)

    @app.post("/api/launch")
    def launch(event: Launch, device: str | None = None):
        return selected(device).launch(event.bundle_id)

    @app.post("/api/unlock")
    def unlock(device: str | None = None):
        return selected(device).unlock()

    @app.post("/api/install")
    def install(file: UploadFile = File(...), device: str | None = None):
        target = selected(device)
        if not file.filename or not file.filename.casefold().endswith(".ipa"):
            raise HTTPException(status_code=422, detail="Choose an IPA file")
        with tempfile.TemporaryDirectory(prefix="limulator-install-") as temporary:
            path = Path(temporary) / "application.ipa"
            size = 0
            with path.open("xb") as stream:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_IPA_BYTES:
                        raise HTTPException(status_code=413, detail="IPA exceeds 512 MiB")
                    stream.write(chunk)
            return target.install(path)

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
