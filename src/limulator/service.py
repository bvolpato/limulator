from __future__ import annotations

import hashlib
import io
import json
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

from .applications import prepared_ipa
from .lifecycle import Runtime
from .profiles import profile_for, public_metadata
from .protocols import ProtocolError


class DeviceError(RuntimeError):
    pass


KEY_NAMES = frozenset(
    {
        "return",
        "enter",
        "delete",
        "backspace",
        "tab",
        "escape",
        "space",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
    }
)


_MAX_CAPTURE_PIXELS = 40_000_000
_ROTATION_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def _parse_ppm_header(header: bytes) -> tuple[int, int]:
    dimensions = re.fullmatch(rb"P6\n([1-9][0-9]{0,9}) ([1-9][0-9]{0,9})\n255\n", header)
    if dimensions is None:
        raise DeviceError("QEMU returned an invalid PPM screenshot header")
    width, height = (int(token) for token in dimensions.groups())
    if width * height > _MAX_CAPTURE_PIXELS:
        raise DeviceError("Guest framebuffer exceeds the supported capture size")
    return width, height


def _read_ppm_dimensions(path: Path) -> tuple[int, int]:
    """Read only the 3-line P6 PPM header to obtain guest framebuffer dimensions."""
    with path.open("rb") as stream:
        header = b"".join(stream.readline(64) for _ in range(3))
    return _parse_ppm_header(header)


def _read_qmp_ppm_raw(path: Path) -> tuple[int, int, int, bytes]:
    """Validate and read QMP's P6 screenshot, returning (width, height, stride, payload)."""
    with path.open("rb") as stream:
        # QEMU writes exactly these three lines. Bound parsing before reading pixels.
        header = b"".join(stream.readline(64) for _ in range(3))
        width, height = _parse_ppm_header(header)

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

    return width, height, stride, payload


def _decode_qmp_ppm(path: Path) -> Image.Image:
    """Decode QMP's P6 screenshot, stripping pixman's per-row alignment bytes."""
    width, height, stride, payload = _read_qmp_ppm_raw(path)
    return Image.frombytes("RGB", (width, height), payload, "raw", "RGB", stride)


def _transpose_rotation(image: Image.Image, rotation: int) -> Image.Image:
    transpose = _ROTATION_TRANSPOSE.get(rotation)
    if transpose is None:
        return image
    rotated = image.transpose(transpose)
    image.close()
    return rotated


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


@dataclass(frozen=True)
class StreamFrame:
    jpeg: bytes
    raw_width: int
    raw_height: int
    rotation: int
    changed: bool = True

    @property
    def width(self) -> int:
        return self.raw_height if self.rotation % 180 else self.raw_width

    @property
    def height(self) -> int:
        return self.raw_width if self.rotation % 180 else self.raw_height

    @property
    def identity(self) -> str:
        return f"{self.raw_width}x{self.raw_height}:{self.rotation}"


def bundle_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value):
        raise DeviceError("Invalid application bundle identifier")
    return value


class DeviceService:
    """Serialize input and folding against the geometry of real captured frames."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._frame: Frame | None = None
        self._orientation: dict = {}
        self._orientation_at = 0.0
        self._needs_orientation = False
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state: str | None = None
        self._last_png_key: tuple[bytes, int, int, int] | None = None
        self._last_jpeg_key: tuple[bytes, int, int, int, int, int | None] | None = None
        self._last_jpeg: bytes | None = None

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

    @contextmanager
    def _pointer_interaction(self, timeout: float = 1.0):
        self._check_available()
        if not self.lock.acquire(timeout=timeout):
            raise DeviceError("Device is busy. Wait for its current operation to finish")
        try:
            self._check_available()
            yield
        finally:
            self.lock.release()

    def _clear_frame_cache(self) -> None:
        with self._state_lock:
            self._frame = None
            self._last_png_key = None
            self._last_jpeg_key = None
            self._last_jpeg = None

    def _change_runtime(self, operation: str) -> dict:
        if not self._lifecycle_lock.acquire(blocking=False):
            raise DeviceError("A start or stop request is already in progress for this device")
        acquired = False
        try:
            acquired = self.lock.acquire(blocking=False)
            if not acquired:
                raise DeviceError("Device is busy. Wait for its current operation to finish")
            self._lifecycle_state = "booting" if operation == "start" else "stopping"
            self._clear_frame_cache()
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
                with self._state_lock:
                    frame = self._frame
                    orientation = self._orientation
                if frame:
                    result["device"].update(
                        width=frame.width,
                        height=frame.height,
                        frame_id=frame.identity,
                    )
                if "fold_angle" in orientation:
                    result["device"]["fold_angle"] = orientation["fold_angle"]
            finally:
                self.lock.release()
        return result

    def _rotation(self) -> int:
        now = time.monotonic()
        if self._needs_orientation or now - self._orientation_at > 1:
            needs_orientation = self._needs_orientation
            try:
                timeout = 30 if needs_orientation else 2
                self._orientation = self.runtime.guest(timeout=timeout).rpc("display.orientation")
                self._needs_orientation = False
            except (OSError, RuntimeError):
                if needs_orientation:
                    raise DeviceError(
                        "Waiting for the guest to confirm its new display orientation"
                    ) from None
            else:
                now = time.monotonic()
            self._orientation_at = now
        if (self._orientation.get("fold_angle") or 0) >= 90:
            return 90
        degrees = self._orientation.get("degrees")
        return int(degrees) % 360 if degrees in (0, 90, 180, 270) else 0

    def capture(self, output: Path | None = None) -> Frame:
        with self._interaction():
            with self._capture_lock:
                with tempfile.TemporaryDirectory(prefix="limulator-frame-") as temporary:
                    raw = Path(temporary) / "screen.ppm"
                    with self.runtime.qmp() as qmp:
                        qmp.screenshot(raw, image_format="ppm")
                    width, height, stride, payload = _read_qmp_ppm_raw(raw)
                rotation = self._rotation()
                digest = hashlib.blake2b(payload, digest_size=16).digest()
                png_key = (digest, width, height, rotation)
                with self._state_lock:
                    cached = (
                        self._frame
                        if self._last_png_key == png_key and self._frame is not None and self._frame.png
                        else None
                    )
                if cached is not None:
                    frame = cached
                else:
                    image = Image.frombytes("RGB", (width, height), payload, "raw", "RGB", stride)
                    try:
                        image = _transpose_rotation(image, rotation)
                        encoded = io.BytesIO()
                        image.save(encoded, format="PNG", compress_level=1)
                    finally:
                        image.close()
                    frame = Frame(encoded.getvalue(), width, height, rotation)
                    with self._state_lock:
                        self._frame = frame
                        self._last_png_key = png_key
                if output is not None:
                    output = output.expanduser().resolve()
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("xb") as stream:
                        stream.write(frame.png)
                return frame

    def capture_jpeg(self, *, quality: int = 80, max_dim: int | None = None) -> StreamFrame:
        """Capture a fast JPEG frame for live streaming, skipping PIL work when pixels are unchanged."""
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 95:
            raise DeviceError("JPEG quality must be an integer between 1 and 95")
        if max_dim is not None and (
            isinstance(max_dim, bool) or not isinstance(max_dim, int) or max_dim < 64
        ):
            raise DeviceError("max_dim must be an integer of at least 64")
        self._check_available()
        with self._capture_lock:
            self._check_available()
            with tempfile.TemporaryDirectory(prefix="limulator-frame-") as temporary:
                raw = Path(temporary) / "screen.ppm"
                with self.runtime.qmp() as qmp:
                    qmp.screenshot(raw, image_format="ppm")
                width, height, stride, payload = _read_qmp_ppm_raw(raw)
            rotation = self._rotation()
            digest = hashlib.blake2b(payload, digest_size=16).digest()
            jpeg_key = (digest, width, height, rotation, quality, max_dim)
            identity = f"{width}x{height}:{rotation}"
            with self._state_lock:
                if self._frame is None or self._frame.identity != identity:
                    self._frame = Frame(b"", width, height, rotation)
                    self._last_png_key = None
                if self._last_jpeg_key == jpeg_key and self._last_jpeg is not None:
                    return StreamFrame(self._last_jpeg, width, height, rotation, changed=False)
            image = Image.frombytes("RGB", (width, height), payload, "raw", "RGB", stride)
            try:
                image = _transpose_rotation(image, rotation)
                if max_dim is not None and max(image.size) > max_dim:
                    image.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)
                encoded = io.BytesIO()
                image.save(encoded, format="JPEG", quality=quality)
                jpeg_bytes = encoded.getvalue()
            finally:
                image.close()
            with self._state_lock:
                self._last_jpeg_key = jpeg_key
                self._last_jpeg = jpeg_bytes
            return StreamFrame(jpeg_bytes, width, height, rotation, changed=True)

    def _probe_geometry(self) -> Frame:
        with tempfile.TemporaryDirectory(prefix="limulator-frame-") as temporary:
            raw = Path(temporary) / "screen.ppm"
            with self.runtime.qmp() as qmp:
                qmp.screenshot(raw, image_format="ppm")
            width, height = _read_ppm_dimensions(raw)
        rotation = self._rotation()
        frame = Frame(b"", width, height, rotation)
        with self._state_lock:
            self._frame = frame
            self._last_png_key = None
        return frame

    def _current_frame(self, identity: str | None) -> Frame:
        with self._state_lock:
            frame = self._frame
        if frame is None:
            if identity is not None:
                raise DeviceError("The display changed. Refresh the screen before sending input")
            frame = self._probe_geometry()
        if identity is not None and identity != frame.identity:
            raise DeviceError("Input refers to an older display size. Refresh the screen")
        return frame

    def tap(self, x: float, y: float, frame_id: str | None = None) -> dict:
        with self._interaction():
            frame = self._current_frame(frame_id)
            raw_x, raw_y = frame.raw_point(x, y)
            with self.runtime.qmp() as qmp:
                qmp.tap(raw_x, raw_y, frame.raw_width, frame.raw_height)
            return {"sent": True}

    def pointer_down(self, x: float, y: float, frame_id: str | None = None) -> dict:
        with self._pointer_interaction():
            frame = self._current_frame(frame_id)
            raw_x, raw_y = frame.raw_point(x, y)
            with self.runtime.qmp() as qmp:
                qmp.pointer_down(raw_x, raw_y, frame.raw_width, frame.raw_height)
            return {"sent": True}

    def pointer_move(self, x: float, y: float, frame_id: str | None = None) -> dict:
        with self._pointer_interaction():
            frame = self._current_frame(frame_id)
            raw_x, raw_y = frame.raw_point(x, y)
            with self.runtime.qmp() as qmp:
                qmp.pointer_move(raw_x, raw_y, frame.raw_width, frame.raw_height)
            return {"sent": True}

    def pointer_up(
        self,
        x: float | None = None,
        y: float | None = None,
        frame_id: str | None = None,
    ) -> dict:
        with self._pointer_interaction():
            if x is not None or y is not None:
                if x is None or y is None:
                    raise DeviceError("Pointer release requires both x and y when coordinates are provided")
                frame = self._current_frame(frame_id)
                raw_x, raw_y = frame.raw_point(x, y)
                with self.runtime.qmp() as qmp:
                    qmp.pointer_up(raw_x, raw_y, frame.raw_width, frame.raw_height)
            else:
                with self.runtime.qmp() as qmp:
                    qmp.pointer_up()
            return {"sent": True}

    def tap_element(self, identifier: str, *, timeout: float = 60) -> dict:
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 1024 or "\0" in identifier:
            raise DeviceError("An accessibility identifier of 1 to 1024 characters is required")
        with self._interaction():
            return self.runtime.guest(timeout=timeout).rpc(
                "ui.tap_element", {"identifier": identifier, "match": "exact"}
            )

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
            return self.runtime.guest().rpc("input.button", {"name": "power" if name == "lock" else name})

    def key(self, name: str) -> dict:
        if not isinstance(name, str) or name not in KEY_NAMES:
            raise DeviceError("Unknown keyboard key")
        with self._interaction():
            return self.runtime.guest().rpc("input.key", {"name": name})

    def fold(self, position: str) -> dict:
        if not self.supports_fold:
            raise DeviceError("Fold posture is only available on iPhone Duo")
        if position not in {"closed", "book", "open"}:
            raise DeviceError("Fold position must be closed, book, or open")
        with self._interaction():
            self._clear_frame_cache()
            self._orientation_at = 0
            self._needs_orientation = True
            guest = self.runtime.guest(timeout=60)
            if position == "closed" and (self._orientation.get("fold_angle") or 0) >= 90:
                guest.rpc("display.posture", {"position": "book"})
                time.sleep(1.5)
            result = guest.rpc("display.posture", {"position": position})
            return result

    def hinge_status(self, timeout: float = 60) -> dict | None:
        """Read the pinned Duo hinge provider's status without changing guest preferences."""
        if not self.supports_fold:
            raise DeviceError("Hinge status is only available on iPhone Duo")
        with self._interaction():
            reply = self.runtime.guest(timeout=timeout).rpc(
                "settings.get",
                {"domain": "com.apple.backboardd", "key": "VPhoneHingeStatus"},
            )
        if not isinstance(reply, dict) or type(reply.get("exists")) is not bool:
            raise DeviceError("Guest returned an invalid hinge status response")
        if not reply["exists"]:
            return None
        if not isinstance(reply.get("value"), dict):
            raise DeviceError("Guest returned a malformed hinge status value")
        return reply["value"]

    def unlock(self) -> dict:
        method_started = time.monotonic()
        with self._interaction():
            guest = self.runtime.guest(timeout=120)
            try:
                return guest.rpc("screen.unlock", {"timeout": 60})
            except ProtocolError as exc:
                if exc.status_code != 400 or not isinstance(exc.details, bytes) or len(exc.details) > 512:
                    raise
                try:
                    response = json.loads(exc.details)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise exc
                error = response.get("error") if isinstance(response, dict) else None
                if (
                    not isinstance(response, dict)
                    or response.get("type") != "response"
                    or not isinstance(error, dict)
                    or error.get("code") != "invalid_operation"
                    or error.get("message") != "the Lock Screen did not dismiss in time"
                ):
                    raise

            deadline = min(method_started + 135, time.monotonic() + 45)

            def rpc(method: str, params: dict | None = None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError(
                        "Touch unlock did not dismiss the Lock Screen before its fallback deadline; unlock it manually"
                    )
                return self.runtime.guest(timeout=remaining).rpc(method, params)

            def screen_state() -> tuple[bool, bool]:
                lock = rpc("notify.state", {"name": "com.apple.springboard.lockstate"})
                blanked = rpc("notify.state", {"name": "com.apple.springboard.hasBlankedScreen"})
                if (
                    not isinstance(lock, dict)
                    or type(lock.get("state")) is not int
                    or lock["state"] < 0
                    or not isinstance(blanked, dict)
                    or type(blanked.get("state")) is not int
                    or blanked["state"] < 0
                ):
                    raise DeviceError("Guest returned invalid lock screen state")
                return lock["state"] != 0, blanked["state"] != 0

            def wait_for_state(*, wait_for_unlock: bool = False) -> tuple[bool, bool]:
                while True:
                    locked, screen_off = screen_state()
                    if not screen_off and (not wait_for_unlock or not locked):
                        return locked, screen_off
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        message = (
                            "Touch unlock did not dismiss the Lock Screen; unlock it manually"
                            if wait_for_unlock
                            else "Touch unlock could not wake the display; unlock it manually"
                        )
                        raise DeviceError(message)
                    time.sleep(min(0.15, remaining))

            locked, screen_off = screen_state()
            was_locked, was_screen_off = locked, screen_off
            if not locked and not screen_off:
                return {
                    "locked": False,
                    "screen_off": False,
                    "was_locked": was_locked,
                    "was_screen_off": was_screen_off,
                }

            width = height = None
            if locked:
                dimensions = rpc("device.screen")
                if not isinstance(dimensions, dict):
                    raise DeviceError("Guest returned invalid screen geometry")
                width, height = dimensions.get("width"), dimensions.get("height")

                def valid_dimension(value) -> bool:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return False
                    try:
                        return math.isfinite(float(value)) and value > 0
                    except (OverflowError, ValueError):
                        return False

                if any(not valid_dimension(value) for value in (width, height)):
                    raise DeviceError("Guest returned invalid screen geometry")

            rpc("input.button", {"name": "wake"})
            locked, screen_off = wait_for_state()
            if locked:
                if width is None or height is None:
                    raise DeviceError("Lock state changed during wake; unlock the device manually")
                rpc(
                    "input.swipe",
                    {
                        "x1": width / 2,
                        "y1": height * 0.96,
                        "x2": width / 2,
                        "y2": height * 0.30,
                        "seconds": 0.7,
                        "steps": 20,
                    },
                )
                locked, screen_off = wait_for_state(wait_for_unlock=True)

            return {
                "locked": locked,
                "screen_off": screen_off,
                "was_locked": was_locked,
                "was_screen_off": was_screen_off,
            }

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
            with prepared_ipa(path, guest_os=self.runtime.device.os_version) as (ipa, info):
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

    def trim_services(
        self,
        *,
        profile: str = "trimmed",
        allow: list[str] | None = None,
        force: bool = True,
    ) -> dict:
        """Apply a trimmed guest launchd service profile to cut idle emulated CPU usage."""
        allowed = ["com.apple.generativeexperiencesd"] if allow is None else list(allow)
        with self._interaction():
            return self.runtime.guest(timeout=120).rpc(
                "services.profile.apply",
                {"profile": profile, "allow": allowed, "force": force},
            )
