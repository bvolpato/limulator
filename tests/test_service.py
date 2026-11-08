import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

import limulator.service as service_module
from limulator.protocols import ProtocolError
from limulator.service import DeviceError, DeviceService, Frame


class Guest:
    def __init__(self):
        self.calls = []
        self.angle = 0
        self.degrees = 0

    def rpc(self, method, params=None):
        self.calls.append((method, params))
        return {"degrees": self.degrees, "fold_angle": self.angle}


class Peer:
    def __init__(self):
        self.calls = []
        self.capture_formats = []
        self.ppm_payload = None

    def screenshot(self, path, *, image_format="png"):
        self.capture_formats.append(image_format)
        if image_format == "ppm" and self.ppm_payload is not None:
            with open(path, "wb") as stream:
                stream.write(self.ppm_payload)
            return
        image = Image.new("RGB", (4, 6))
        image.putpixel((0, 0), (255, 0, 0))
        image.save(path, format=image_format.upper())

    def tap(self, *args):
        self.calls.append(args)


class Runtime:
    def __init__(self):
        self.device = SimpleNamespace(
            name="duo", model="iPhone19,4", os_version="27.1", width=1398, height=2034, scale=3, cid=41
        )
        self.peer = Peer()
        self.client = Guest()

    def guest(self, timeout=60):
        return self.client

    @contextmanager
    def qmp(self):
        yield self.peer


def _ppm_payload(rows, *, row_padding=b""):
    height = len(rows)
    width = len(rows[0])
    packed_rows = [bytes(channel for pixel in row for channel in pixel) for row in rows]
    return f"P6\n{width} {height}\n255\n".encode() + b"".join(row + row_padding for row in packed_rows)


@pytest.mark.parametrize(
    ("button", "guest_button"),
    [("home", "home"), ("lock", "power"), ("volume-up", "volume-up"), ("volume-down", "volume-down")],
)
def test_button_maps_lock_to_the_guest_power_action(button, guest_button):
    runtime = Runtime()

    DeviceService(runtime).button(button)

    assert runtime.client.calls == [("input.button", {"name": guest_button})]


def test_folded_frame_rotation_and_tap_use_the_same_physical_geometry():
    runtime = Runtime()
    runtime.client.angle = 180
    service = DeviceService(runtime)
    frame = service.capture()
    assert (frame.width, frame.height, frame.rotation) == (6, 4, 90)
    with Image.open(io.BytesIO(frame.png)) as image:
        assert image.getpixel((5, 0)) == (255, 0, 0)
    service.tap(5, 0, frame.identity)
    assert runtime.peer.calls == [(0, 0, 4, 6)]


def test_capture_compresses_raw_guest_pixels_to_png_outside_qemu():
    runtime = Runtime()
    frame = DeviceService(runtime).capture()

    assert runtime.peer.capture_formats == ["ppm"]
    with Image.open(io.BytesIO(frame.png)) as image:
        assert image.format == "PNG"
        assert image.size == (4, 6)
        expected = Image.new("RGB", (4, 6))
        expected.putpixel((0, 0), (255, 0, 0))
        assert image.tobytes() == expected.tobytes()


def test_capture_removes_pixman_padding_from_1398_pixel_rows():
    width = 1398
    rows = [
        [(10, 20, 30), *([(220, 10, 10)] * (width - 1))],
        [(20, 190, 40)] * width,
    ]
    runtime = Runtime()
    runtime.peer.ppm_payload = _ppm_payload(rows, row_padding=b"\xfa\xfb")

    frame = DeviceService(runtime).capture()

    with Image.open(io.BytesIO(frame.png)) as image:
        assert image.size == (width, 2)
        assert image.getpixel((0, 0)) == (10, 20, 30)
        assert image.getpixel((1, 0)) == (220, 10, 10)
        assert image.getpixel((0, 1)) == (20, 190, 40)
        assert image.getpixel((width - 1, 1)) == (20, 190, 40)


def test_capture_strips_padded_rows_before_applying_rotation():
    rows = [
        [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
        [(255, 255, 0), (255, 0, 255), (0, 255, 255)],
    ]
    runtime = Runtime()
    runtime.client.degrees = 90
    runtime.peer.ppm_payload = _ppm_payload(rows, row_padding=b"\xfa\xfb\xfc")

    frame = DeviceService(runtime).capture()

    assert (frame.width, frame.height, frame.rotation) == (2, 3, 90)
    with Image.open(io.BytesIO(frame.png)) as image:
        expected = [
            (255, 255, 0),
            (255, 0, 0),
            (255, 0, 255),
            (0, 255, 0),
            (0, 255, 255),
            (0, 0, 255),
        ]
        assert image.tobytes() == bytes(channel for pixel in expected for channel in pixel)


@pytest.mark.parametrize("width", [3, 4])
def test_capture_accepts_packed_rows_for_padded_and_aligned_widths(width):
    rows = [
        [(10, 20, 30)] + [(200, 10, 10)] * (width - 1),
        [(20, 190, 40)] * width,
    ]
    runtime = Runtime()
    runtime.peer.ppm_payload = _ppm_payload(rows)

    frame = DeviceService(runtime).capture()

    with Image.open(io.BytesIO(frame.png)) as image:
        assert image.size == (width, 2)
        assert image.tobytes() == bytes(channel for row in rows for pixel in row for channel in pixel)


def test_capture_rejects_truncated_ppm_payload():
    runtime = Runtime()
    runtime.peer.ppm_payload = _ppm_payload([[(1, 2, 3), (4, 5, 6)]])[:-1]

    with pytest.raises(DeviceError, match="truncated or malformed PPM"):
        DeviceService(runtime).capture()


@pytest.mark.parametrize(
    "header,error",
    [
        (b"P6\n0 2\n255\n", "invalid PPM screenshot header"),
        (b"P6\n7000 7000\n255\n", "exceeds the supported capture size"),
    ],
)
def test_capture_validates_dimensions_before_reading_pixels(header, error):
    runtime = Runtime()
    runtime.peer.ppm_payload = header

    with pytest.raises(DeviceError, match=error):
        DeviceService(runtime).capture()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_transformed_subpixel_edges_remain_in_the_physical_display(rotation):
    frame = Frame(b"", 4, 6, rotation)
    for x, y in ((0, 0), (frame.width - 0.1, frame.height - 0.1)):
        raw_x, raw_y = frame.raw_point(x, y)
        assert 0 <= raw_x < frame.raw_width
        assert 0 <= raw_y < frame.raw_height


@pytest.mark.parametrize("x,y", [(-1, 0), (4, 0), (0, 6), (float("nan"), 0), (0, float("inf"))])
def test_outside_or_nonfinite_input_is_rejected_before_it_reaches_qemu(x, y):
    with pytest.raises(DeviceError):
        Frame(b"", 4, 6, 0).raw_point(x, y)


def test_posture_change_rejects_input_against_the_old_frame():
    runtime = Runtime()
    service = DeviceService(runtime)
    frame = service.capture()
    service.fold("open")
    with pytest.raises(DeviceError, match="display changed"):
        service.tap(1, 1, frame.identity)
    assert runtime.peer.calls == []
    assert ("display.posture", {"position": "open"}) in runtime.client.calls


def test_capture_preserves_an_existing_output(tmp_path):
    output = tmp_path / "screen.png"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        DeviceService(Runtime()).capture(output)
    assert output.read_bytes() == b"existing"


def test_invalid_bundle_identifier_does_not_reach_guest():
    runtime = Runtime()
    with pytest.raises(DeviceError):
        DeviceService(runtime).launch("unsafe\nidentifier")
    assert runtime.client.calls == []


def test_screen_queries_native_point_bounds_with_the_requested_timeout():
    runtime = Runtime()
    timeouts = []

    def guest(timeout=60):
        timeouts.append(timeout)
        return runtime.client

    runtime.guest = guest
    DeviceService(runtime).screen(timeout=0.75)
    assert timeouts == [0.75]
    assert runtime.client.calls == [("device.screen", None)]


def test_hinge_status_reads_the_duo_provider_value_with_the_requested_timeout():
    runtime = Runtime()
    status = {
        "enumerated": True,
        "pid": 42,
        "updated_at": 123.5,
        "current_angle": 100,
        "sweeping": False,
        "dispatch_failures": 0,
        "error": "",
        "configuration": {"enabled": True, "angle": 100},
    }
    calls = []
    timeouts = []
    runtime.client.rpc = lambda method, params=None: calls.append((method, params)) or {
        "domain": "com.apple.backboardd",
        "user": "mobile",
        "key": "VPhoneHingeStatus",
        "exists": True,
        "value": status,
        "type": "plist",
    }

    def guest(timeout=60):
        timeouts.append(timeout)
        return runtime.client

    runtime.guest = guest

    assert DeviceService(runtime).hinge_status(timeout=0.75) == status
    assert timeouts == [0.75]
    assert calls == [
        ("settings.get", {"domain": "com.apple.backboardd", "key": "VPhoneHingeStatus"})
    ]


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {"values": {}},
        {"exists": 1, "value": {}},
        {"exists": True, "value": "not a dictionary"},
    ],
)
def test_hinge_status_rejects_malformed_settings_replies(reply):
    runtime = Runtime()
    runtime.client.rpc = lambda *_args, **_kwargs: reply

    with pytest.raises(DeviceError, match="hinge status"):
        DeviceService(runtime).hinge_status()


def test_hinge_status_reports_a_missing_provider_without_fabricating_state():
    runtime = Runtime()
    runtime.client.rpc = lambda *_args, **_kwargs: {
        "domain": "com.apple.backboardd",
        "user": "mobile",
        "key": "VPhoneHingeStatus",
        "exists": False,
        "value": None,
        "type": "unknown",
    }

    assert DeviceService(runtime).hinge_status() is None


def test_unlock_allows_sixty_seconds_in_the_guest_with_a_longer_host_deadline():
    runtime = Runtime()
    timeouts = []

    def guest(timeout=60):
        timeouts.append(timeout)
        return runtime.client

    runtime.guest = guest
    DeviceService(runtime).unlock()
    assert timeouts == [120]
    assert runtime.client.calls == [("screen.unlock", {"timeout": 60})]


def _no_passcode_unlock_error(
    *, code="invalid_operation", message="the Lock Screen did not dismiss in time", status=400
):
    body = {
        "error": {"code": code, "message": message},
        "id": 1,
        "type": "response",
    }
    return ProtocolError("screen unlock failed", status_code=status, details=json.dumps(body).encode())


def _fallback_runtime(*, lock_states, blanked_states, geometry=None, before_unlock_error=None):
    runtime = Runtime()
    state_sequences = {
        "com.apple.springboard.lockstate": list(lock_states),
        "com.apple.springboard.hasBlankedScreen": list(blanked_states),
    }
    state_indexes = dict.fromkeys(state_sequences, 0)

    def rpc(method, params=None):
        runtime.client.calls.append((method, params))
        if method == "screen.unlock":
            if before_unlock_error is not None:
                before_unlock_error()
            raise _no_passcode_unlock_error()
        if method == "notify.state":
            name = params["name"]
            values = state_sequences[name]
            index = state_indexes[name]
            state_indexes[name] = min(index + 1, len(values) - 1)
            return {"state": values[index]}
        if method == "device.screen":
            return geometry if geometry is not None else {"width": 393, "height": 852}
        return {"sent": True}

    runtime.client.rpc = rpc
    return runtime


def test_unlock_uses_one_touch_swipe_after_waking_a_dark_lock_screen():
    runtime = _fallback_runtime(lock_states=[1, 1, 0], blanked_states=[1, 0, 0])

    result = DeviceService(runtime).unlock()

    assert result == {"locked": False, "screen_off": False, "was_locked": True, "was_screen_off": True}
    calls = runtime.client.calls
    assert [method for method, _ in calls if method.startswith("input.")] == ["input.button", "input.swipe"]
    assert calls[4] == ("input.button", {"name": "wake"})
    assert calls[7] == (
        "input.swipe",
        {"x1": 196.5, "y1": 817.92, "x2": 196.5, "y2": 255.6, "seconds": 0.7, "steps": 20},
    )


def test_unlock_wakes_a_lit_lock_screen_before_swiping():
    runtime = _fallback_runtime(lock_states=[1, 1, 0], blanked_states=[0, 0, 0])

    result = DeviceService(runtime).unlock()

    assert result == {"locked": False, "screen_off": False, "was_locked": True, "was_screen_off": False}
    assert [method for method, _ in runtime.client.calls if method.startswith("input.")] == [
        "input.button",
        "input.swipe",
    ]
    assert runtime.client.calls[4] == ("input.button", {"name": "wake"})
    assert runtime.client.calls[5:7] == [
        ("notify.state", {"name": "com.apple.springboard.lockstate"}),
        ("notify.state", {"name": "com.apple.springboard.hasBlankedScreen"}),
    ]
    assert runtime.client.calls[7][0] == "input.swipe"


def test_unlock_returns_without_geometry_or_input_when_already_unlocked_and_lit():
    runtime = _fallback_runtime(lock_states=[0], blanked_states=[0])

    result = DeviceService(runtime).unlock()

    assert result == {"locked": False, "screen_off": False, "was_locked": False, "was_screen_off": False}
    assert not any(
        method == "device.screen" or method.startswith("input.") for method, _ in runtime.client.calls
    )


@pytest.mark.parametrize(
    ("code", "message", "status"),
    [
        ("invalid_operation", "a passcode is required", 400),
        ("invalid_operation", "another operation failed", 400),
        ("invalid_operation", "the Lock Screen did not dismiss in time", 503),
    ],
)
def test_unlock_propagates_other_errors_without_touch_input(code, message, status):
    runtime = Runtime()
    error = _no_passcode_unlock_error(code=code, message=message, status=status)

    def rpc(method, params=None):
        runtime.client.calls.append((method, params))
        raise error

    runtime.client.rpc = rpc
    with pytest.raises(ProtocolError) as caught:
        DeviceService(runtime).unlock()

    assert caught.value is error
    assert runtime.client.calls == [("screen.unlock", {"timeout": 60})]


def test_unlock_rejects_invalid_geometry_before_sending_input():
    runtime = _fallback_runtime(lock_states=[1], blanked_states=[1], geometry={"width": 0, "height": 852})

    with pytest.raises(DeviceError, match="invalid screen geometry"):
        DeviceService(runtime).unlock()

    assert not any(method.startswith("input.") for method, _ in runtime.client.calls)


@pytest.mark.parametrize(
    ("elapsed_before_error", "expected_deadline", "fallback_budget"), [(0, 45, 45), (100, 135, 35)]
)
def test_unlock_persistent_lock_fallback_is_bounded(
    monkeypatch, elapsed_before_error, expected_deadline, fallback_budget
):
    clock = {"now": 0.0}
    timeouts = []
    runtime = _fallback_runtime(
        lock_states=[1],
        blanked_states=[0],
        before_unlock_error=lambda: clock.__setitem__("now", elapsed_before_error),
    )

    def monotonic():
        return clock["now"]

    def sleep(duration):
        clock["now"] += duration

    def guest(timeout=60):
        timeouts.append(timeout)
        return runtime.client

    runtime.guest = guest
    monkeypatch.setattr(service_module.time, "monotonic", monotonic)
    monkeypatch.setattr(service_module.time, "sleep", sleep)

    with pytest.raises(DeviceError, match="did not dismiss the Lock Screen"):
        DeviceService(runtime).unlock()

    assert clock["now"] == expected_deadline
    assert all(0 < timeout <= fallback_budget for timeout in timeouts[1:])
    assert runtime.client.calls[4] == ("input.button", {"name": "wake"})
    assert sum(method == "input.swipe" for method, _ in runtime.client.calls) == 1


def test_phone_cannot_send_fold_commands_to_guest():
    runtime = Runtime()
    runtime.device.model = "iPhone17,3"
    service = DeviceService(runtime)
    with pytest.raises(DeviceError, match="only available on iPhone Duo"):
        service.fold("open")
    with pytest.raises(DeviceError, match="only available on iPhone Duo"):
        service.hinge_status()
    assert runtime.client.calls == []


def test_stop_rejects_duplicates_and_input_without_queueing():
    runtime = Runtime()
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def stop():
        calls.append("stop")
        entered.set()
        assert finish.wait(3)
        return {"state": "stopped"}

    runtime.stop = stop
    runtime.status = lambda: {"state": "running", "device": {}}
    service = DeviceService(runtime)
    service._frame = Frame(b"old", 4, 6, 0)
    worker = threading.Thread(target=service.stop)
    worker.start()
    try:
        assert entered.wait(3)
        assert service.status()["state"] == "stopping"
        with pytest.raises(DeviceError, match="already in progress"):
            service.stop()
        with pytest.raises(DeviceError, match="stopping"):
            service.tap(1, 1)
        assert calls == ["stop"]
        assert runtime.peer.calls == []
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive()
    assert service._frame is None


def test_start_requests_nonblocking_boot_and_invalidates_previous_frame():
    runtime = Runtime()
    calls = []
    runtime.start = lambda **kwargs: calls.append(kwargs) or {"state": "booting"}
    service = DeviceService(runtime)
    service._frame = Frame(b"old", 4, 6, 0)
    assert service.start() == {"state": "booting"}
    assert calls == [{"wait_timeout": 0}]
    assert service._frame is None


@pytest.mark.parametrize("operation", ["capture", "start", "stop"])
def test_busy_device_refuses_new_operations_without_queueing(operation):
    runtime = Runtime()
    calls = []
    runtime.start = lambda **kwargs: calls.append("start") or {"state": "booting"}
    runtime.stop = lambda: calls.append("stop") or {"state": "stopped"}
    service = DeviceService(runtime)
    entered, finish = threading.Event(), threading.Event()

    def hold_operation():
        with service._interaction():
            entered.set()
            assert finish.wait(3)

    with ThreadPoolExecutor(max_workers=2) as workers:
        holder = workers.submit(hold_operation)
        assert entered.wait(1)
        try:
            pending = workers.submit(getattr(service, operation))
            with pytest.raises(DeviceError, match="busy"):
                pending.result(timeout=0.2)
            assert calls == []
        finally:
            finish.set()
            holder.result(timeout=1)
    getattr(service, operation)()
    assert calls == ([] if operation == "capture" else [operation])


def test_status_returns_runtime_metadata_while_an_operation_holds_the_frame_lock():
    runtime = Runtime()
    runtime.status = lambda: {"state": "running", "device": {}}
    service = DeviceService(runtime)
    service._frame = Frame(b"old", 4, 6, 90)
    entered, finish = threading.Event(), threading.Event()

    def hold_operation():
        with service._interaction():
            entered.set()
            assert finish.wait(3)

    with ThreadPoolExecutor(max_workers=2) as workers:
        holder = workers.submit(hold_operation)
        assert entered.wait(1)
        try:
            status = workers.submit(service.status).result(timeout=0.2)
            assert status["state"] == "running"
            assert (status["device"]["width"], status["device"]["height"]) == (1398, 2034)
        finally:
            finish.set()
            holder.result(timeout=1)
