from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from limulator import protocols
from limulator.protocols import GuestClient, ProtocolError, QMPClient


class UnixServer:
    def __init__(self, handler: Callable[[socket.socket], None], tmp_path: Path) -> None:
        self.path = tmp_path / "fake.sock"
        self.path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.error: BaseException | None = None

        def run() -> None:
            try:
                connection, _ = self.listener.accept()
                with connection:
                    handler(connection)
            except BaseException as exc:
                self.error = exc
            finally:
                self.listener.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def finish(self) -> None:
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "fake socket server did not finish"
        if self.error:
            raise self.error


def _read_until(sock: socket.socket, marker: bytes, maximum: int = 1 << 20) -> bytes:
    data = bytearray()
    while marker not in data:
        # Read one byte at a time here so an HTTP header read cannot consume the
        # following body from the socket before the test starts its body reader.
        chunk = sock.recv(1)
        if not chunk:
            raise AssertionError("client closed before sending the complete frame")
        data.extend(chunk)
        if len(data) > maximum:
            raise AssertionError("test request exceeded its frame bound")
    return bytes(data)


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(min(65536, count - len(data)))
        if not chunk:
            raise AssertionError("client closed before sending the full body")
        data.extend(chunk)
    if len(data) > count:
        setattr(sock, "_test_buffer", bytearray(data[count:]))
    return bytes(data[:count])


def _drip(sock: socket.socket, data: bytes, interval: float) -> None:
    pause = threading.Event()
    for byte in data:
        try:
            sock.sendall(bytes((byte,)))
        except OSError:
            return
        pause.wait(interval)


def _http_request(sock: socket.socket) -> tuple[str, dict[str, str], bytes]:
    header = _read_until(sock, b"\r\n\r\n")[:-4]
    lines = header.decode("ascii").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()
    body = _read_exact(sock, int(headers.get("content-length", "0")))
    return f"{method} {path}", headers, body


def _uds_http_handler(reply: bytes, *, fragments: tuple[bytes, ...] = ()) -> Callable[[socket.socket], None]:
    def handler(sock: socket.socket) -> None:
        assert _read_until(sock, b"\n") == b"CONNECT 1339\n"
        sock.sendall(b"OK 1339\n")
        request, headers, body = _http_request(sock)
        assert request
        assert headers["host"] == "localhost"
        assert len(body) == int(headers["content-length"])
        if fragments:
            for fragment in fragments:
                sock.sendall(fragment)
        else:
            sock.sendall(reply)

    return handler


def test_guest_request_handles_split_uds_handshake_headers_and_body(tmp_path: Path) -> None:
    response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\nhello world"
    fragments = (response[:4], response[4:17], response[17:43], response[43:])
    server = UnixServer(_uds_http_handler(response, fragments=fragments), tmp_path)

    result = GuestClient(unix_socket=server.path).request("GET", "/v1/health")
    server.finish()

    assert result == b"hello world"


@pytest.mark.parametrize("slow_part", ["headers", "body"])
def test_guest_request_uses_one_deadline_for_dribbled_response(tmp_path: Path, slow_part: str) -> None:
    def handler(sock: socket.socket) -> None:
        assert _read_until(sock, b"\n") == b"CONNECT 1339\n"
        sock.sendall(b"OK 1339\n")
        _http_request(sock)
        if slow_part == "headers":
            _drip(sock, b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n", 0.015)
            return
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n")
        _drip(sock, b"12345678", 0.025)

    server = UnixServer(handler, tmp_path)
    started = time.monotonic()
    with pytest.raises(ProtocolError, match="timed out while reading HTTP response") as error:
        GuestClient(unix_socket=server.path, timeout=0.09).request("GET", "/v1/health")
    elapsed = time.monotonic() - started
    server.finish()

    assert isinstance(error.value.__cause__, TimeoutError)
    assert elapsed < 0.6


def test_guest_request_raises_with_http_status_and_body(tmp_path: Path) -> None:
    reply = b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nnot found"
    server = UnixServer(_uds_http_handler(reply), tmp_path)

    with pytest.raises(ProtocolError, match="HTTP 404 Not Found: not found") as error:
        GuestClient(unix_socket=server.path).request("GET", "/missing")
    server.finish()

    assert error.value.status_code == 404
    assert error.value.details == b"not found"


def test_guest_rpc_returns_result_and_raises_rpc_error(tmp_path: Path) -> None:
    success = json.dumps({"id": 1, "result": {"device": "iPhone"}}).encode()
    first = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(success)).encode() + b"\r\n\r\n" + success
    server = UnixServer(_uds_http_handler(first), tmp_path)

    assert GuestClient(unix_socket=server.path).rpc("device.snapshot") == {"device": "iPhone"}
    server.finish()

    failure = json.dumps({"id": 1, "error": {"message": "guest rejected request"}}).encode()
    second = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(failure)).encode() + b"\r\n\r\n" + failure
    server = UnixServer(_uds_http_handler(second), tmp_path)
    with pytest.raises(ProtocolError, match="guest rejected request"):
        GuestClient(unix_socket=server.path).rpc("apps.install", {"path": "/tmp/app.ipa"})
    server.finish()


def test_guest_request_file_streams_large_upload(tmp_path: Path) -> None:
    archive = tmp_path / "app.ipa"
    block = bytes(range(256)) * 4096
    expected = hashlib.sha256()
    with archive.open("wb") as output:
        for _ in range(8):
            output.write(block)
            expected.update(block)
    size = archive.stat().st_size
    seen: dict[str, object] = {}

    def handler(sock: socket.socket) -> None:
        assert _read_until(sock, b"\n") == b"CONNECT 1339\n"
        sock.sendall(b"OK 1339\n")
        header = _read_until(sock, b"\r\n\r\n")[:-4]
        lines = header.decode("ascii").split("\r\n")
        assert lines[0] == "PUT /v1/files/content?path=/tmp/app.ipa HTTP/1.1"
        headers = {name.lower(): value.strip() for name, value in (line.split(":", 1) for line in lines[1:])}
        assert headers["content-type"] == "application/octet-stream"
        remaining = int(headers["content-length"])
        digest = hashlib.sha256()
        received = 0
        while remaining:
            chunk = sock.recv(min(65536, remaining))
            if not chunk:
                raise AssertionError("streaming upload ended early")
            received += len(chunk)
            remaining -= len(chunk)
            digest.update(chunk)
        seen["size"] = received
        seen["digest"] = digest.hexdigest()
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    server = UnixServer(handler, tmp_path)
    result = GuestClient(unix_socket=server.path, timeout=10).request_file(
        "PUT", "/v1/files/content?path=/tmp/app.ipa", archive
    )
    server.finish()

    assert result == b"ok"
    assert seen == {"size": size, "digest": expected.hexdigest()}
    assert size > 4 << 20


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="AF_VSOCK is Linux-specific")
def test_vsock_buffer_options_accept_native_uint64_on_kernel_socket() -> None:
    af_vsock = getattr(socket, "AF_VSOCK", 40)
    try:
        sock = socket.socket(af_vsock, socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"host kernel does not provide AF_VSOCK: {exc}")
    with sock:
        protocols._set_vsock_buffers(sock, af_vsock)
        for option in (
            getattr(socket, "SO_VM_SOCKETS_BUFFER_MAX_SIZE", 2),
            getattr(socket, "SO_VM_SOCKETS_BUFFER_SIZE", 0),
        ):
            if hasattr(socket, "AF_VSOCK"):
                size = sock.getsockopt(af_vsock, option)
            else:
                raw = sock.getsockopt(af_vsock, option, struct.calcsize("@Q"))
                size = struct.unpack("@Q", raw)[0]
            assert size == 4 << 20


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="AF_VSOCK is Linux-specific")
def test_guest_connects_to_kernel_vsock_loopback_without_a_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        listener = socket.socket(getattr(socket, "AF_VSOCK", 40), socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"host kernel does not provide AF_VSOCK: {exc}")
    with listener:
        listener.settimeout(1)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        libc.bind.restype = ctypes.c_int
        libc.getsockname.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        libc.getsockname.restype = ctypes.c_int
        libc.accept4.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        libc.accept4.restype = ctypes.c_int
        # Linux sockaddr_vm: family, reserved, port, CID, flags, three zero bytes.
        address = ctypes.create_string_buffer(struct.pack("@HHIIB3x", 40, 0, 0xFFFFFFFF, 1, 0))
        if libc.bind(listener.fileno(), address, 16) != 0:
            error = ctypes.get_errno()
            pytest.skip(f"host kernel cannot bind AF_VSOCK loopback: errno {error}")
        length = ctypes.c_uint(16)
        assert libc.getsockname(listener.fileno(), address, ctypes.byref(length)) == 0
        family, _, port, cid, _ = struct.unpack("@HHIIB3x", address.raw[:16])
        assert family == 40 and cid == 1
        listener.listen(1)
        monkeypatch.setattr(protocols, "_VPHONED_PORT", port)
        sock, _ = GuestClient(cid=1, timeout=1)._connect(time.monotonic() + 1)
        with sock:
            # The managed build also cannot decode an address in socket.accept().
            descriptor = libc.accept4(listener.fileno(), None, None, socket.SOCK_CLOEXEC)
            assert descriptor >= 0, ctypes.get_errno()
            with socket.socket(40, socket.SOCK_STREAM, fileno=descriptor) as peer:
                peer.settimeout(1)
                sock.sendall(b"local vsock")
                assert peer.recv(32) == b"local vsock"


@pytest.fixture
def pending_vsock_connect(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(
        now=100.0, initial_error=errno.EINPROGRESS, socket_error=0, ready=True, interrupted=False, waits=[]
    )

    class Socket:
        timeout = None

        def setblocking(self, blocking):
            assert blocking is False

        def fileno(self):
            return 10

        def getsockopt(self, level, option):
            assert (level, option) == (socket.SOL_SOCKET, socket.SO_ERROR)
            return state.socket_error

        def settimeout(self, value):
            self.timeout = value

    def connect(descriptor, address, length):
        assert descriptor == 10
        assert struct.unpack("@HHIIB3x", ctypes.string_at(address, length)) == (40, 0, 1339, 1, 0)
        ctypes.set_errno(state.initial_error)
        return -1 if state.initial_error else 0

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, sock, event):
            assert event == protocols.selectors.EVENT_WRITE

        def select(self, timeout):
            state.waits.append(timeout)
            state.now += min(timeout, 0.04)
            if state.interrupted:
                raise InterruptedError
            return [(None, protocols.selectors.EVENT_WRITE)] if state.ready else []

    monkeypatch.setattr(protocols.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(connect=connect))
    monkeypatch.setattr(protocols.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(protocols.time, "monotonic", lambda: state.now)
    return Socket(), state


@pytest.mark.parametrize("initial_error", [0, errno.EINPROGRESS, errno.EINTR])
def test_linux_vsock_connect_handles_immediate_and_async_completion(pending_vsock_connect, initial_error):
    sock, state = pending_vsock_connect
    state.initial_error = initial_error
    protocols._connect_vsock_linux(sock, 1, 1339, state.now + 0.1)
    assert 0 < sock.timeout <= 0.1 + 1e-9
    assert bool(state.waits) == bool(initial_error)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_linux_vsock_connect_reports_syscall_and_pending_socket_errors(pending_vsock_connect, asynchronous):
    sock, state = pending_vsock_connect
    state.initial_error = errno.EINPROGRESS if asynchronous else errno.EACCES
    state.socket_error = errno.ECONNREFUSED
    with pytest.raises(OSError) as error:
        protocols._connect_vsock_linux(sock, 1, 1339, state.now + 0.1)
    assert error.value.errno == (errno.ECONNREFUSED if asynchronous else errno.EACCES)
    assert sock.timeout is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_linux_vsock_pending_connect_obeys_one_deadline_even_with_interrupted_waits(
    pending_vsock_connect, interrupted
):
    sock, state = pending_vsock_connect
    state.ready = False
    state.interrupted = interrupted
    deadline = state.now + 0.1
    with pytest.raises(ProtocolError, match="timed out while connecting") as error:
        protocols._connect_vsock_linux(sock, 1, 1339, deadline)
    assert isinstance(error.value.__cause__, TimeoutError)
    assert state.now == deadline
    assert len(state.waits) == 3
    assert state.waits[0] > state.waits[1] > state.waits[2] > 0


def _read_qmp_line(sock: socket.socket) -> dict[str, object]:
    line = bytearray()
    while not line.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("QMP client closed before sending a command")
        line.extend(chunk)
        if len(line) > 1 << 20:
            raise AssertionError("QMP command exceeded test frame bound")
    return json.loads(line)


def _send_qmp(sock: socket.socket, payload: dict[str, object], *, partial: bool = False) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode() + b"\r\n"
    if partial:
        for offset in range(0, len(data), 3):
            sock.sendall(data[offset : offset + 3])
    else:
        sock.sendall(data)


def test_qmp_capture_keeps_png_default_and_supports_uncompressed_frames(tmp_path: Path) -> None:
    commands = []

    def handler(sock: socket.socket) -> None:
        _send_qmp(sock, {"QMP": {"version": {}}})
        capabilities = _read_qmp_line(sock)
        _send_qmp(sock, {"return": {}, "id": capabilities["id"]})
        for _ in range(2):
            command = _read_qmp_line(sock)
            commands.append(command)
            _send_qmp(sock, {"return": {}, "id": command["id"]})

    server = UnixServer(handler, tmp_path)
    with QMPClient(server.path) as client:
        client.screenshot(tmp_path / "default.png")
        client.screenshot(tmp_path / "raw.ppm", image_format="ppm")
        with pytest.raises(ValueError, match="PNG or PPM"):
            client.screenshot(tmp_path / "unsupported.jpg", image_format="jpeg")
    server.finish()

    assert [command["execute"] for command in commands] == ["screendump", "screendump"]
    assert [command["arguments"] for command in commands] == [
        {"filename": str(tmp_path / "default.png"), "format": "png"},
        {"filename": str(tmp_path / "raw.ppm"), "format": "ppm"},
    ]


def test_qmp_handles_events_stale_ids_and_maps_passed_screen_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("limulator.protocols.time.sleep", lambda _seconds: None)
    commands: list[dict[str, object]] = []

    def handler(sock: socket.socket) -> None:
        _send_qmp(sock, {"QMP": {"version": {"qemu": {"major": 9}}}}, partial=True)
        capabilities = _read_qmp_line(sock)
        commands.append(capabilities)
        _send_qmp(sock, {"event": "RESET", "data": {}})
        _send_qmp(sock, {"return": {}, "id": capabilities["id"]}, partial=True)
        for _ in range(3):
            command = _read_qmp_line(sock)
            commands.append(command)
            if len(commands) == 2:
                _send_qmp(sock, {"return": {"old": True}, "id": -1})
                _send_qmp(sock, {"event": "BALLOON_CHANGE", "data": {"actual": 1}})
            _send_qmp(sock, {"return": {}, "id": command["id"]})

    server = UnixServer(handler, tmp_path)
    client = QMPClient(server.path)
    with client:
        assert client.events == [{"event": "RESET", "data": {}}]
        client.tap(50, 25, width=200, height=100)
        assert any(event["event"] == "BALLOON_CHANGE" for event in client.events)
    server.finish()

    assert commands[0]["execute"] == "qmp_capabilities"
    position = commands[1]["arguments"]["events"]
    assert position == [
        {"type": "abs", "data": {"axis": "x", "value": 8191}},
        {"type": "abs", "data": {"axis": "y", "value": 8191}},
    ]
    assert commands[2]["arguments"]["events"] == [{"type": "btn", "data": {"down": True, "button": "left"}}]
    assert commands[3]["arguments"]["events"] == [{"type": "btn", "data": {"down": False, "button": "left"}}]


def test_qmp_partial_response_line_obeys_one_command_deadline(tmp_path: Path) -> None:
    def handler(sock: socket.socket) -> None:
        _send_qmp(sock, {"QMP": {"version": {}}})
        capabilities = _read_qmp_line(sock)
        _send_qmp(sock, {"return": {}, "id": capabilities["id"]})
        command = _read_qmp_line(sock)
        body = (
            json.dumps({"return": {"status": "running"}, "id": command["id"]}, separators=(",", ":")).encode()
            + b"\n"
        )
        _drip(sock, body, 0.02)

    server = UnixServer(handler, tmp_path)
    client = QMPClient(server.path, timeout=0.1)
    started = time.monotonic()
    with client, pytest.raises(ProtocolError, match="timed out waiting for QMP response") as error:
        client.command("query-status")
    elapsed = time.monotonic() - started
    server.finish()

    assert isinstance(error.value.__cause__, TimeoutError)
    assert elapsed < 0.6


def test_qmp_reports_remote_errors_and_releases_button_after_gesture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("limulator.protocols.time.sleep", lambda _seconds: None)
    commands: list[dict[str, object]] = []

    def handler(sock: socket.socket) -> None:
        _send_qmp(sock, {"QMP": {"version": {}}})
        capabilities = _read_qmp_line(sock)
        _send_qmp(sock, {"return": {}, "id": capabilities["id"]})
        while True:
            try:
                command = _read_qmp_line(sock)
            except AssertionError:
                return
            commands.append(command)
            if command["id"] == 3:
                _send_qmp(sock, {"error": {"class": "GenericError", "desc": "touch failed"}, "id": 3})
            else:
                _send_qmp(sock, {"return": {}, "id": command["id"]})

    server = UnixServer(handler, tmp_path)
    client = QMPClient(server.path)
    with client, pytest.raises(ProtocolError, match="touch failed"):
        client.tap(0, 0, 100, 100)
    server.finish()

    assert [command["id"] for command in commands] == [2, 3, 4]
    assert commands[-1]["arguments"]["events"] == [{"type": "btn", "data": {"down": False, "button": "left"}}]


def test_qmp_rejects_out_of_bounds_coordinates_before_sending_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("limulator.protocols.time.sleep", lambda _seconds: None)
    commands: list[dict[str, object]] = []

    def handler(sock: socket.socket) -> None:
        _send_qmp(sock, {"QMP": {"version": {}}})
        capabilities = _read_qmp_line(sock)
        commands.append(capabilities)
        _send_qmp(sock, {"return": {}, "id": capabilities["id"]})
        # The client closes after rejecting the coordinate.

    server = UnixServer(handler, tmp_path)
    client = QMPClient(server.path)
    with client, pytest.raises(ValueError, match="x must be in bounds"):
        client.tap(100, 50, 100, 100)
    server.finish()

    assert [command["execute"] for command in commands] == ["qmp_capabilities"]


def test_qmp_reports_closed_socket_during_capabilities(tmp_path: Path) -> None:
    def handler(sock: socket.socket) -> None:
        _send_qmp(sock, {"QMP": {"version": {}}})
        _read_qmp_line(sock)

    server = UnixServer(handler, tmp_path)

    with pytest.raises(ProtocolError, match="connection closed before the response was complete"):
        QMPClient(server.path, timeout=2)
    server.finish()


def test_qmp_rejects_oversized_greeting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("limulator.protocols._MAX_QMP_LINE", 32)

    def handler(sock: socket.socket) -> None:
        sock.sendall(b'{"QMP":{"version":{"build":"' + b"x" * 40 + b'"}}}\n')

    server = UnixServer(handler, tmp_path)

    with pytest.raises(ProtocolError, match="QMP greeting exceeds 32 bytes"):
        QMPClient(server.path, timeout=2)
    server.finish()
