from __future__ import annotations

import io
import math
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from .applications import inspect_app, prepared_ipa
from .lifecycle import Runtime
from .profiles import profile_for, public_metadata


class DeviceError(RuntimeError):
    pass


_MAX_CAPTURE_PIXELS = 40_000_000


def _decode_qmp_ppm(path: Path) -> Image.Image:
    """Decode QMP's P6 screenshot, stripping pixman's per-row alignment bytes."""
    with path.open("rb") as stream:
        # QEMU writes exactly these three lines. Bound parsing before reading pixels.
        header = b"".join(stream.readline(64) for _ in range(3))
        dimensions = re.fullmatch(rb"P6\n([1-9][0-9]{0,9}) ([1-9][0-9]{0,9})\n255\n", header)
        if dimensions is None:
            raise DeviceError("QEMU returned an invalid PPM screenshot header")
        width, height = (int(token) for token in dimensions.groups())
        if width * height > _MAX_CAPTURE_PIXELS:
            raise DeviceError("Guest framebuffer exceeds the supported capture size")

        row_bytes = width * 3
        aligned_row_bytes = (row_bytes + 3) & ~3
        packed_size = row_bytes * height
        aligned_size = aligned_row_bytes * height
        payload_size = path.stat().st_size - len(header)
        if payload_size == packed_size:
            stride = row_bytes
        elif aligned_row_bytes != row_bytes and payload_size == aligned_size:
            stride = aligned_row_bytes
        else:
            raise DeviceError("QEMU returned a truncated or malformed PPM framebuffer")

        payload = stream.read(payload_size)
        if len(payload) != payload_size:
            raise DeviceError("QEMU returned a truncated or malformed PPM framebuffer")

    return Image.frombytes("RGB", (width, height), payload, "raw", "RGB", stride)


def device_metadata(device) -> dict:
    try:
        profile = profile_for(device.model, device.os_version)
    except ValueError:
        profile = None
    return {
        "name": device.name,
        "model": device.model,
        "os_version": device.os_version,
        "width": device.width,
        "height": device.height,
        "scale": device.scale,
        "cid": device.cid,
        "profile": public_metadata(profile) if profile else None,
        "supports_fold": device.model == "iPhone19,4",
    }


@dataclass(frozen=True)
class Frame:
    png: bytes
    raw_width: int
    raw_height: int
    rotation: int

    @property
    def width(self) -> int:
        return self.raw_height if self.rotation % 180 else self.raw_width

    @property
    def height(self) -> int:
        return self.raw_width if self.rotation % 180 else self.raw_height

    @property
    def identity(self) -> str:
        return f"{self.raw_width}x{self.raw_height}:{self.rotation}"

    def raw_point(self, x: float, y: float) -> tuple[float, float]:
        if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x < self.width and 0 <= y < self.height):
            raise DeviceError(f"Input must be within the current {self.width} by {self.height} frame")
        x, y = min(x, self.width - 1), min(y, self.height - 1)
        if self.rotation == 90:
            return y, self.raw_height - 1 - x
        if self.rotation == 180:
            return self.raw_width - 1 - x, self.raw_height - 1 - y
        if self.rotation == 270:
            return self.raw_width - 1 - y, x
        return x, y


def bundle_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value):
        raise DeviceError("Invalid application bundle identifier")
    return value


class DeviceService:
    """Serialize input and folding against the geometry of real captured frames."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self._frame: Frame | None = None
        self._orientation: dict = {}
        self._orientation_at = 0.0
        self._needs_orientation = False
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state: str | None = None

    @property
    def supports_fold(self) -> bool:
        return self.runtime.device.model == "iPhone19,4"

    def _check_available(self) -> None:
        if self._lifecycle_lock.locked():
            raise DeviceError(f"Device is {self._lifecycle_state or 'changing state'}. Wait for it to finish")

    @contextmanager
    def _interaction(self):
        self._check_available()
        if not self.lock.acquire(blocking=False):
            raise DeviceError("Device is busy. Wait for its current operation to finish")
        try:
            self._check_available()
            yield
        finally:
            self.lock.release()

    def _change_runtime(self, operation: str) -> dict:
        if not self._lifecycle_lock.acquire(blocking=False):
            raise DeviceError("A start or stop request is already in progress for this device")
        acquired = False
        try:
            acquired = self.lock.acquire(blocking=False)
            if not acquired:
                raise DeviceError("Device is busy. Wait for its current operation to finish")
            self._lifecycle_state = "booting" if operation == "start" else "stopping"
            self._frame = None
            self._orientation = {}
            self._orientation_at = 0
            self._needs_orientation = False
            if operation == "start":
                return self.runtime.start(wait_timeout=0)
            return self.runtime.stop()
        finally:
            self._lifecycle_state = None
            if acquired:
                self.lock.release()
            self._lifecycle_lock.release()

    def start(self) -> dict:
        return self._change_runtime("start")

    def stop(self) -> dict:
        return self._change_runtime("stop")

    def status(self) -> dict:
        result = self.runtime.status()
        result["device"].update(device_metadata(self.runtime.device))
        if self._lifecycle_state:
            result["state"] = self._lifecycle_state
            return result
        if self.lock.acquire(blocking=False):
            try:
                if self._frame:
                    result["device"].update(width=self._frame.width, height=self._frame.height)
                if "fold_angle" in self._orientation:
                    result["device"]["fold_angle"] = self._orientation["fold_angle"]
            finally:
                self.lock.release()
        return result

    def _rotation(self) -> int:
        now = time.monotonic()
        if now - self._orientation_at > 1:
            try:
                self._orientation = self.runtime.guest(timeout=2).rpc("display.orientation")
                self._needs_orientation = False
            except (OSError, RuntimeError):
                if self._needs_orientation:
                    raise DeviceError(
                        "Waiting for the guest to confirm its new display orientation"
                    ) from None
            self._orientation_at = now
        if (self._orientation.get("fold_angle") or 0) >= 90:
            return 90
        degrees = self._orientation.get("degrees")
        return int(degrees) % 360 if degrees in (0, 90, 180, 270) else 0

    def capture(self, output: Path | None = None) -> Frame:
        with self._interaction():
            with tempfile.TemporaryDirectory(prefix="limulator-frame-") as temporary:
                raw = Path(temporary) / "screen.ppm"
                with self.runtime.qmp() as qmp:
                    qmp.screenshot(raw, image_format="ppm")
                image = _decode_qmp_ppm(raw)
                try:
                    width, height = image.size
                    rotation = self._rotation()
                    if rotation:
                        rotated = image.rotate(-rotation, expand=True)
                        image.close()
                        image = rotated
                    encoded = io.BytesIO()
                    image.save(encoded, format="PNG")
                finally:
                    image.close()
            self._frame = Frame(encoded.getvalue(), width, height, rotation)
            if output is not None:
                output = output.expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(self._frame.png)
            return self._frame

    def _current_frame(self, identity: str | None) -> Frame:
        if self._frame is None:
            if identity is not None:
                raise DeviceError("The display changed. Refresh the screen before sending input")
            self.capture()
        assert self._frame is not None
        if identity is not None and identity != self._frame.identity:
            raise DeviceError("Input refers to an older display size. Refresh the screen")
        return self._frame

    def tap(self, x: float, y: float, frame_id: str | None = None) -> dict:
        with self._interaction():
            frame = self._current_frame(frame_id)
            raw_x, raw_y = frame.raw_point(x, y)
            with self.runtime.qmp() as qmp:
                qmp.tap(raw_x, raw_y, frame.raw_width, frame.raw_height)
            return {"sent": True}

    def tap_element(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 1024 or "\0" in identifier:
            raise DeviceError("An accessibility identifier of 1 to 1024 characters is required")
        with self._interaction():
            return self.runtime.guest().rpc("ui.tap_element", {"identifier": identifier, "match": "exact"})

    def swipe(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        duration_ms: float = 350,
        frame_id: str | None = None,
    ) -> dict:
        if not math.isfinite(duration_ms) or not 50 <= duration_ms <= 5000:
            raise DeviceError("Swipe duration must be between 50 and 5000 milliseconds")
        with self._interaction():
            frame = self._current_frame(frame_id)
            start, end = frame.raw_point(x0, y0), frame.raw_point(x1, y1)
            with self.runtime.qmp() as qmp:
                qmp.swipe(start, end, frame.raw_width, frame.raw_height, duration=duration_ms / 1000)
            return {"sent": True}

    def text(self, text: str) -> dict:
        if not isinstance(text, str) or len(text) > 4096 or "\0" in text:
            raise DeviceError("Text must contain at most 4096 characters and no NUL")
        with self._interaction():
            return self.runtime.guest().rpc("input.type", {"text": text})

    def button(self, name: str) -> dict:
        if name not in {"home", "lock", "volume-up", "volume-down"}:
            raise DeviceError("Unknown device button")
        with self._interaction():
            return self.runtime.guest().rpc("input.button", {"name": name})

    def fold(self, position: str) -> dict:
        if not self.supports_fold:
            raise DeviceError("Fold posture is only available on iPhone Duo")
        if position not in {"closed", "book", "open"}:
            raise DeviceError("Fold position must be closed, book, or open")
        with self._interaction():
            self._frame = None
            self._orientation_at = 0
            self._needs_orientation = True
            guest = self.runtime.guest(timeout=60)
            if position == "closed" and (self._orientation.get("fold_angle") or 0) >= 90:
                guest.rpc("display.posture", {"position": "book"})
                time.sleep(1.5)
            result = guest.rpc("display.posture", {"position": position})
            return result

    def unlock(self) -> dict:
        with self._interaction():
            return self.runtime.guest(timeout=120).rpc("screen.unlock", {"timeout": 60})

    def apps(self) -> list[dict]:
        self._check_available()
        result = self.runtime.guest().rpc("apps.list")
        records = result.get("apps", []) if isinstance(result, dict) else result
        if not isinstance(records, list):
            raise DeviceError("Guest returned an invalid application list")
        apps = []
        for record in records:
            if not isinstance(record, dict):
                continue
            identifier = record.get("bundle_id") or record.get("bundleIdentifier") or record.get("bundleID")
            if isinstance(identifier, str):
                apps.append(
                    {
                        "bundle_id": identifier,
                        "name": record.get("name")
                        or record.get("display_name")
                        or record.get("displayName")
                        or identifier,
                    }
                )
        return sorted(apps, key=lambda app: str(app["name"]).casefold())

    def install(self, path: Path) -> dict:
        with self._interaction():
            info = inspect_app(path, guest_os=self.runtime.device.os_version)
            with prepared_ipa(path) as (ipa, _):
                guest_path = f"/var/root/Library/Caches/limulator-{uuid.uuid4().hex}.ipa"
                guest = self.runtime.guest(timeout=600)
                guest.request_file("PUT", "/v1/files/content?path=" + quote(guest_path, safe=""), ipa)
                result = guest.rpc("apps.install", {"path": guest_path})
            return {"bundle_id": info.bundle_id, "name": info.name, "result": result}

    def launch(self, bundle_id: str, arguments: list[str] | None = None) -> dict:
        bundle_identifier(bundle_id)
        params: dict = {"bundle_id": bundle_id}
        if arguments:
            raise DeviceError("The pinned guest launcher does not support app launch arguments")
        with self._interaction():
            return self.runtime.guest(timeout=120).rpc("apps.launch", params)

    def tree(self, timeout: float = 60) -> dict:
        self._check_available()
        return self.runtime.guest(timeout=timeout).rpc("ui.tree")

    def screen(self, timeout: float = 60) -> dict:
        """Read IcliKit's interface point metrics, which can fall back to the main panel."""
        self._check_available()
        return self.runtime.guest(timeout=timeout).rpc("device.screen")

    def terminate(self, bundle_id: str) -> dict:
        self._check_available()
        return self.runtime.guest().rpc("apps.terminate", {"bundle_id": bundle_identifier(bundle_id)})
