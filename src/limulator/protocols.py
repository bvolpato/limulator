"""Small, bounded clients for vphoned's HTTP API and QEMU's QMP socket."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


class ProtocolError(RuntimeError):
    """A peer returned an invalid response or a transport failed mid-protocol."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


_VPHONED_PORT = 1339
_VSOCK_BUFFER_SIZE = 4 << 20
# Linux's AF_VSOCK ABI value is 40; some Python builds omit the socket constant.
_LINUX_AF_VSOCK = 40
_MAX_HTTP_HEADER = 64 << 10
_MAX_HTTP_BODY = 256 << 20
_MAX_QMP_LINE = 1 << 20
_MAX_QMP_MESSAGES_PER_COMMAND = 1024
_MAX_QMP_EVENTS = 256
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTENT_LENGTH = re.compile(rb"^[0-9]+$")


def _validated_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a finite positive number")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a finite positive number")
    return value


def _remaining(deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        cause = TimeoutError(f"deadline expired while {operation}")
        raise ProtocolError(f"timed out while {operation}") from cause
    return remaining


def _set_socket_deadline(sock: socket.socket, deadline: float, operation: str) -> None:
    remaining = _remaining(deadline, operation)
    try:
        sock.settimeout(remaining)
    except OSError as exc:
        raise ProtocolError(f"could not set timeout while {operation}: {exc}") from exc


def _sendall(sock: socket.socket, data: bytes, deadline: float, operation: str) -> None:
    _set_socket_deadline(sock, deadline, operation)
    try:
        sock.sendall(data)
    except TimeoutError as exc:
        raise ProtocolError(f"timed out while {operation}") from exc
    except OSError as exc:
        raise ProtocolError(f"socket write failed while {operation}: {exc}") from exc


def _set_vsock_buffers(sock: socket.socket, af_vsock: int) -> None:
    # These vm_sockets options take native uint64_t values, not Python's int optlen.
    value = struct.pack("@Q", _VSOCK_BUFFER_SIZE)
    for option in (
        getattr(socket, "SO_VM_SOCKETS_BUFFER_MAX_SIZE", 2),
        getattr(socket, "SO_VM_SOCKETS_BUFFER_SIZE", 0),
    ):
        sock.setsockopt(af_vsock, option, value)


class _SocketBuffer:
    """Read bounded frames while retaining bytes received past a frame boundary."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()

    def _recv(self, deadline: float | None, what: str) -> None:
        if deadline is not None:
            _set_socket_deadline(self.sock, deadline, f"reading {what}")
        try:
            chunk = self.sock.recv(65536)
        except TimeoutError as exc:
            raise ProtocolError(f"timed out while reading {what}") from exc
        except OSError as exc:
            raise ProtocolError(f"socket read failed while reading {what}: {exc}") from exc
        if not chunk:
            raise ProtocolError("connection closed before the response was complete")
        self.buffer.extend(chunk)

    def read_line(self, maximum: int, *, what: str, deadline: float | None = None) -> bytes:
        while True:
            if deadline is not None:
                _remaining(deadline, f"reading {what}")
            end = self.buffer.find(b"\n")
            if end >= 0:
                if end + 1 > maximum:
                    raise ProtocolError(f"{what} exceeds {maximum} bytes")
                line = bytes(self.buffer[: end + 1])
                del self.buffer[: end + 1]
                return line
            if len(self.buffer) >= maximum:
                raise ProtocolError(f"{what} exceeds {maximum} bytes")
            self._recv(deadline, what)

    def read_until(
        self,
        marker: bytes,
        maximum: int,
        *,
        what: str,
        deadline: float | None = None,
    ) -> bytes:
        while True:
            if deadline is not None:
                _remaining(deadline, f"reading {what}")
            end = self.buffer.find(marker)
            if end >= 0:
                frame_size = end + len(marker)
                if frame_size > maximum:
                    raise ProtocolError(f"{what} exceeds {maximum} bytes")
                frame = bytes(self.buffer[:frame_size])
                del self.buffer[:frame_size]
                return frame
            if len(self.buffer) >= maximum:
                raise ProtocolError(f"{what} exceeds {maximum} bytes")
            self._recv(deadline, what)

    def read_exactly(
        self, count: int, *, deadline: float | None = None, what: str = "response body"
    ) -> bytes:
        while len(self.buffer) < count:
            if deadline is not None:
                _remaining(deadline, f"reading {what}")
            self._recv(deadline, what)
        if deadline is not None:
            _remaining(deadline, f"reading {what}")
        frame = bytes(self.buffer[:count])
        del self.buffer[:count]
        return frame


class GuestClient:
    """Call vphoned over AF_VSOCK or its optional Unix-socket bridge."""

    def __init__(
        self,
        cid: int = 3,
        timeout: float = 60,
        unix_socket: Path | None = None,
    ) -> None:
        if isinstance(cid, bool) or not isinstance(cid, int) or not 0 <= cid <= 0xFFFFFFFF:
            raise ValueError("cid must be an integer between 0 and 4294967295")
        self.cid = cid
        self.timeout = _validated_timeout(timeout)
        self.unix_socket = Path(unix_socket) if unix_socket is not None else None
        self._rpc_id = 0
        self._rpc_id_lock = threading.Lock()

    def _connect(self, deadline: float) -> tuple[socket.socket, _SocketBuffer]:
        if self.unix_socket is not None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                _set_socket_deadline(sock, deadline, "connecting to vphoned UDS")
                sock.connect(os.fspath(self.unix_socket))
                _sendall(
                    sock,
                    f"CONNECT {_VPHONED_PORT}\n".encode("ascii"),
                    deadline,
                    "sending the vphoned UDS handshake",
                )
                reader = _SocketBuffer(sock)
                line = reader.read_line(4096, what="vsock UDS handshake", deadline=deadline)
                try:
                    handshake = line.decode("ascii").strip()
                except UnicodeDecodeError as exc:
                    raise ProtocolError("vsock UDS handshake is not ASCII") from exc
                if not re.fullmatch(r"OK(?:\s+[0-9]+)?", handshake):
                    raise ProtocolError(f"vsock UDS handshake failed: {handshake!r}")
                return sock, reader
            except ProtocolError:
                sock.close()
                raise
            except TimeoutError as exc:
                sock.close()
                raise ProtocolError(f"timed out connecting to vphoned UDS {self.unix_socket}") from exc
            except OSError as exc:
                sock.close()
                raise ProtocolError(f"could not connect to vphoned UDS {self.unix_socket}: {exc}") from exc

        af_vsock = getattr(socket, "AF_VSOCK", _LINUX_AF_VSOCK if sys.platform.startswith("linux") else None)
        if af_vsock is None:
            raise ProtocolError("AF_VSOCK is not available on this host")
        sock = socket.socket(af_vsock, socket.SOCK_STREAM)
        try:
            _set_vsock_buffers(sock, af_vsock)
            _set_socket_deadline(sock, deadline, f"connecting to vphoned at CID {self.cid}")
            sock.connect((self.cid, _VPHONED_PORT))
            return sock, _SocketBuffer(sock)
        except ProtocolError:
            sock.close()
            raise
        except TimeoutError as exc:
            sock.close()
            raise ProtocolError(f"timed out connecting to vphoned at CID {self.cid}") from exc
        except OSError as exc:
            sock.close()
            raise ProtocolError(f"could not connect to vphoned at CID {self.cid}: {exc}") from exc

    @staticmethod
    def _validate_request(method: str, path: str, content_type: str) -> bytes:
        if not isinstance(method, str) or not _HTTP_TOKEN.fullmatch(method):
            raise ValueError("method must be an HTTP token")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or any(ord(char) <= 32 or ord(char) == 127 for char in path)
            or "#" in path
        ):
            raise ValueError("path must be an absolute HTTP request target without controls or fragments")
        if not isinstance(content_type, str) or "\r" in content_type or "\n" in content_type:
            raise ValueError("content_type must not contain newlines")
        try:
            return f"{method} {path} HTTP/1.1\r\n".encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("path must be ASCII; percent-encode non-ASCII characters") from exc

    @staticmethod
    def _request_headers(start: bytes, content_length: int, content_type: str) -> bytes:
        if any(ord(char) < 32 or ord(char) == 127 for char in content_type):
            raise ValueError("content_type must not contain control characters")
        try:
            type_header = f"Content-Type: {content_type}\r\n".encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("content_type must be ASCII") from exc
        headers = start + b"Host: localhost\r\nConnection: close\r\n"
        headers += f"Content-Length: {content_length}\r\n".encode("ascii")
        headers += type_header + b"\r\n"
        if len(headers) > _MAX_HTTP_HEADER:
            raise ValueError(f"request headers exceed {_MAX_HTTP_HEADER} bytes")
        return headers

    def _read_response(self, method: str, reader: _SocketBuffer, deadline: float) -> bytes:
        _remaining(deadline, "processing the vphoned HTTP response")
        raw = reader.read_until(
            b"\r\n\r\n",
            _MAX_HTTP_HEADER,
            what="HTTP response headers",
            deadline=deadline,
        )
        lines = raw[:-4].split(b"\r\n")
        if not lines:
            raise ProtocolError("HTTP response has no status line")
        try:
            status_line = lines[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolError("HTTP status line is not ASCII") from exc
        status_parts = status_line.split(" ", 2)
        if (
            len(status_parts) < 2
            or status_parts[0] not in ("HTTP/1.0", "HTTP/1.1")
            or not re.fullmatch(r"[0-9]{3}", status_parts[1])
        ):
            raise ProtocolError(f"invalid HTTP status line: {status_line!r}")
        status = int(status_parts[1])
        if not 100 <= status <= 599:
            raise ProtocolError(f"invalid HTTP status code: {status}")
        reason = status_parts[2] if len(status_parts) == 3 else ""

        headers: dict[str, bytes] = {}
        for line in lines[1:]:
            if not line or line[:1] in (b" ", b"\t") or b":" not in line:
                raise ProtocolError(f"invalid HTTP response header: {line!r}")
            name, value = line.split(b":", 1)
            try:
                header_name = name.decode("ascii").lower()
            except UnicodeDecodeError as exc:
                raise ProtocolError("HTTP header name is not ASCII") from exc
            if not _HTTP_TOKEN.fullmatch(header_name):
                raise ProtocolError(f"invalid HTTP response header name: {header_name!r}")
            if header_name in headers:
                raise ProtocolError(f"duplicate HTTP response header: {header_name}")
            headers[header_name] = value.strip()

        transfer_encoding = headers.get("transfer-encoding", b"").lower()
        if transfer_encoding not in (b"", b"identity"):
            raise ProtocolError(
                f"unsupported HTTP transfer-encoding: {transfer_encoding.decode('ascii', 'replace')}"
            )
        no_body = method.upper() == "HEAD" or status in (204, 304)
        if "content-length" not in headers:
            if no_body:
                content_length = 0
            else:
                raise ProtocolError("HTTP response is missing Content-Length")
        else:
            raw_length = headers["content-length"]
            if not _CONTENT_LENGTH.fullmatch(raw_length) or len(raw_length) > 10:
                raise ProtocolError(f"invalid HTTP Content-Length: {raw_length!r}")
            declared_length = int(raw_length)
            content_length = 0 if no_body else declared_length
        if content_length > _MAX_HTTP_BODY:
            raise ProtocolError(f"HTTP response body exceeds {_MAX_HTTP_BODY} bytes")
        body = (
            reader.read_exactly(content_length, deadline=deadline, what="HTTP response body")
            if content_length
            else b""
        )
        _remaining(deadline, "processing the vphoned HTTP response")
        if not 200 <= status < 300:
            detail = body[:512].decode("utf-8", "replace")
            suffix = f": {detail}" if detail else ""
            raise ProtocolError(
                f"HTTP {status} {reason}{suffix}".rstrip(),
                status_code=status,
                details=body,
            )
        return body

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> bytes:
        """Send one HTTP request, keeping the socket open until the reply is read."""
        deadline = time.monotonic() + self.timeout
        start = self._validate_request(method, path, content_type)
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        headers = self._request_headers(start, len(body), content_type)
        sock, reader = self._connect(deadline)
        try:
            _sendall(sock, headers, deadline, "writing HTTP request headers")
            if body:
                _sendall(sock, body, deadline, "writing HTTP request body")
            return self._read_response(method, reader, deadline)
        finally:
            sock.close()

    def request_file(
        self,
        method: str,
        path: str,
        file: Path,
        content_type: str = "application/octet-stream",
    ) -> bytes:
        """Stream a regular file as the request body without loading it into memory."""
        deadline = time.monotonic() + self.timeout
        start = self._validate_request(method, path, content_type)
        source_path = Path(file)
        with source_path.open("rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("request_file requires a regular file")
            content_length = info.st_size
            headers = self._request_headers(start, content_length, content_type)
            sock, reader = self._connect(deadline)
            try:
                _sendall(sock, headers, deadline, "writing HTTP request headers")
                remaining = content_length
                while remaining:
                    _remaining(deadline, "reading the request file")
                    try:
                        chunk = source.read(min(1 << 20, remaining))
                    except OSError as exc:
                        raise ProtocolError(f"could not read request file: {exc}") from exc
                    _remaining(deadline, "reading the request file")
                    if not chunk:
                        raise ProtocolError("request file ended before its advertised Content-Length")
                    _sendall(sock, chunk, deadline, "writing HTTP request file body")
                    remaining -= len(chunk)
                return self._read_response(method, reader, deadline)
            finally:
                sock.close()

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a vphoned RPC and return its result, raising on an RPC error."""
        if not isinstance(method, str) or not method:
            raise ValueError("RPC method must be a non-empty string")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise TypeError("RPC params must be a dictionary")
        with self._rpc_id_lock:
            self._rpc_id += 1
            request_id = self._rpc_id
        payload = json.dumps(
            {"method": method, "params": params, "id": request_id},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        raw = self.request("POST", "/v1/rpc", payload, "application/json")
        try:
            reply = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"vphoned RPC returned invalid JSON: {exc}") from exc
        if not isinstance(reply, dict):
            raise ProtocolError("vphoned RPC response must be a JSON object", details=reply)
        if "id" in reply and reply["id"] != request_id:
            raise ProtocolError(
                f"vphoned RPC response id {reply['id']!r} does not match request id {request_id}",
                details=reply,
            )
        if "error" in reply and reply["error"] is not None:
            error = reply["error"]
            if isinstance(error, dict):
                message = error.get("message", error)
            else:
                message = error
            raise ProtocolError(f"vphoned RPC {method} failed: {message}", details=error)
        return reply.get("result", reply)


class QMPClient:
    """A synchronous QMP client that preserves asynchronous events."""

    def __init__(self, path: str | os.PathLike[str], timeout: float = 60) -> None:
        self.path = Path(path)
        self.timeout = _validated_timeout(timeout)
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._reader = _SocketBuffer(self._socket)
        self._command_lock = threading.Lock()
        self._next_id = 0
        self.events: list[dict[str, Any]] = []
        self._closed = False
        deadline = time.monotonic() + self.timeout
        try:
            _set_socket_deadline(self._socket, deadline, f"connecting to QMP socket {self.path}")
            self._socket.connect(os.fspath(self.path))
            greeting = self._receive_json(deadline, what="QMP greeting")
            if not isinstance(greeting, dict) or not isinstance(greeting.get("QMP"), dict):
                raise ProtocolError("QMP server sent an invalid greeting")
            self.greeting = greeting
            self.command("qmp_capabilities")
        except TimeoutError as exc:
            self.close()
            raise ProtocolError(f"timed out connecting to QMP socket {self.path}") from exc
        except OSError as exc:
            self.close()
            raise ProtocolError(f"could not connect to QMP socket {self.path}: {exc}") from exc
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> QMPClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._socket.close()
            except OSError:
                pass

    def _receive_json(self, deadline: float, *, what: str = "QMP response") -> Any:
        try:
            line = self._reader.read_line(_MAX_QMP_LINE, what=what, deadline=deadline).rstrip(b"\r\n")
        except ProtocolError as exc:
            if isinstance(exc.__cause__, TimeoutError):
                raise ProtocolError(f"timed out waiting for {what}") from exc.__cause__
            raise
        try:
            return json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"QMP sent invalid JSON: {exc}") from exc

    def _send_json(self, payload: dict[str, Any], deadline: float, *, what: str = "QMP command") -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > _MAX_QMP_LINE:
            raise ProtocolError(f"QMP command exceeds {_MAX_QMP_LINE} bytes")
        _sendall(self._socket, encoded, deadline, f"writing {what}")

    def command(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._closed:
            raise ProtocolError("QMP client is closed")
        if not isinstance(name, str) or not name:
            raise ValueError("QMP command name must be a non-empty string")
        if arguments is not None and not isinstance(arguments, dict):
            raise TypeError("QMP arguments must be a dictionary")
        deadline = time.monotonic() + self.timeout
        remaining = _remaining(deadline, "waiting to issue QMP command")
        if not self._command_lock.acquire(timeout=remaining):
            cause = TimeoutError("deadline expired while waiting for QMP command lock")
            raise ProtocolError("timed out waiting for QMP command lock") from cause
        try:
            _remaining(deadline, "issuing QMP command")
            self._next_id += 1
            request_id = self._next_id
            payload: dict[str, Any] = {"execute": name, "id": request_id}
            if arguments is not None:
                payload["arguments"] = arguments
            self._send_json(payload, deadline, what=f"QMP command {name}")
            for _ in range(_MAX_QMP_MESSAGES_PER_COMMAND):
                reply = self._receive_json(deadline)
                if not isinstance(reply, dict):
                    raise ProtocolError("QMP response must be a JSON object", details=reply)
                if "event" in reply:
                    self.events.append(reply)
                    if len(self.events) > _MAX_QMP_EVENTS:
                        del self.events[: len(self.events) - _MAX_QMP_EVENTS]
                    continue
                if "id" not in reply:
                    raise ProtocolError("QMP command response is missing its id", details=reply)
                if reply["id"] != request_id:
                    # A delayed duplicate reply is not the answer to the command in flight.
                    continue
                if "error" in reply:
                    raise ProtocolError(
                        f"QMP command {name} failed: {self._qmp_error_message(reply['error'])}",
                        details=reply["error"],
                    )
                if "return" not in reply:
                    raise ProtocolError("QMP response has neither return nor error", details=reply)
                return reply["return"]
            raise ProtocolError("too many stale QMP messages while waiting for a response")
        finally:
            self._command_lock.release()

    @staticmethod
    def _qmp_error_message(error: Any) -> str:
        if isinstance(error, dict):
            if "desc" in error:
                return str(error["desc"])
            if "class" in error:
                return str(error["class"])
        return str(error)

    def screenshot(self, path: str | os.PathLike[str], *, image_format: str = "png") -> Any:
        filename = os.fspath(path)
        if not isinstance(filename, str):
            raise TypeError("screenshot path must be a string or text PathLike")
        if image_format not in ("png", "ppm"):
            raise ValueError("screenshot format must be PNG or PPM")
        return self.command("screendump", {"filename": filename, "format": image_format})

    @staticmethod
    def _coordinate(value: float, extent: float, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number")
        if isinstance(extent, bool) or not isinstance(extent, (int, float)):
            raise ValueError(f"{name.rsplit('.', 1)[0]} extent must be a finite number")
        try:
            value_f, extent_f = float(value), float(extent)
        except OverflowError as exc:
            raise ValueError(f"{name} and its extent must be finite numbers") from exc
        if not math.isfinite(value_f):
            raise ValueError(f"{name} must be a finite number")
        if not math.isfinite(extent_f):
            raise ValueError(f"{name.rsplit('.', 1)[0]} extent must be a finite number")
        if extent_f <= 0:
            raise ValueError(f"{name.rsplit('.', 1)[0]} extent must be positive")
        if value_f < 0 or value_f >= extent_f:
            raise ValueError(f"{name} must be in bounds [0, {extent_f})")
        return int(value_f / extent_f * 32767)

    @classmethod
    def _absolute_events(
        cls,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> list[dict[str, Any]]:
        px = cls._coordinate(x, width, "x")
        py = cls._coordinate(y, height, "y")
        return [
            {"type": "abs", "data": {"axis": "x", "value": px}},
            {"type": "abs", "data": {"axis": "y", "value": py}},
        ]

    def _send_events(self, events: list[dict[str, Any]]) -> Any:
        return self.command("input-send-event", {"events": events})

    @staticmethod
    def _button_event(button: str, down: bool) -> dict[str, Any]:
        return {"type": "btn", "data": {"down": down, "button": button}}

    def _press_and_release(self, button: str, action: Callable[[], None]) -> None:
        try:
            # Release even if the down command times out after QEMU received it.
            self._send_events([self._button_event(button, True)])
            action()
        except BaseException:
            try:
                self._send_events([self._button_event(button, False)])
            except Exception:
                pass
            raise
        else:
            self._send_events([self._button_event(button, False)])

    def tap(self, x: float, y: float, width: float, height: float) -> None:
        position = self._absolute_events(x, y, width, height)
        self._send_events(position)
        time.sleep(0.05)
        self._press_and_release("left", lambda: time.sleep(0.12))

    def swipe(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        width: float,
        height: float,
        duration: float = 0.3,
        steps: int = 20,
    ) -> None:
        if len(start) != 2 or len(end) != 2:
            raise ValueError("swipe start and end must each contain x and y")
        start_events = self._absolute_events(start[0], start[1], width, height)
        self._absolute_events(end[0], end[1], width, height)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError("duration must be a finite non-negative number")
        duration_f = float(duration)
        if not math.isfinite(duration_f) or duration_f < 0:
            raise ValueError("duration must be a finite non-negative number")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise ValueError("steps must be a positive integer")

        self._send_events(start_events)
        time.sleep(0.05)

        def drag() -> None:
            for step in range(1, steps + 1):
                ratio = step / steps
                x = start[0] + (end[0] - start[0]) * ratio
                y = start[1] + (end[1] - start[1]) * ratio
                self._send_events(self._absolute_events(x, y, width, height))
                if duration_f:
                    time.sleep(duration_f / steps)

        self._press_and_release("left", drag)

    def home(self) -> None:
        self._press_and_release("middle", lambda: time.sleep(0.1))
