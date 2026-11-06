import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette import formparsers

from limulator import server
from limulator.server import create_app
from limulator.service import DeviceError, Frame


class Service:
    def __init__(self):
        self.inputs = []

    def status(self):
        return {"state": "stopped", "device": {}, "backend": {"ready": False}}

    def tap(self, x, y, frame_id):
        self.inputs.append((x, y, frame_id))
        return {"sent": True}


@pytest.fixture
def client():
    service = Service()
    with TestClient(
        create_app(service, token="test-session-token"), base_url="http://127.0.0.1:8765"
    ) as client:
        yield client, service


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Limulator-Token": "wrong"},
        {"X-Limulator-Token": "test-session-token", "Origin": "https://other.example"},
        {"X-Limulator-Token": "test-session-token", "Host": "other.example"},
    ],
)
def test_browser_cannot_control_guest_without_session_and_same_origin(client, headers):
    client, service = client
    response = client.post("/api/input", headers=headers, json={"type": "tap", "x": 1, "y": 2})
    assert response.status_code == 403
    assert service.inputs == []


def test_authorized_tap_preserves_frame_identity(client):
    client, service = client
    response = client.post(
        "/api/input",
        headers={"X-Limulator-Token": "test-session-token"},
        json={"type": "tap", "x": 1, "y": 2, "frame_id": "4x6:0"},
    )
    assert response.status_code == 200
    assert service.inputs == [(1, 2, "4x6:0")]


def test_status_is_honest_when_the_guest_is_stopped(client):
    client, _ = client
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    assert response.headers["cache-control"] == "no-store"


def test_non_ascii_token_is_rejected_without_a_server_error(client):
    client, service = client
    response = client.post(
        "/api/input", headers=[(b"x-limulator-token", b"\xff")], json={"type": "tap", "x": 1, "y": 2}
    )
    assert response.status_code == 403
    assert service.inputs == []


def test_nonfinite_browser_input_is_rejected(client):
    client, service = client
    response = client.post(
        "/api/input",
        headers={"X-Limulator-Token": "test-session-token", "Content-Type": "application/json"},
        content='{"type":"tap","x":NaN,"y":0}',
    )
    assert response.status_code == 422
    assert service.inputs == []


@pytest.mark.parametrize(
    "body",
    [
        {"type": "tap"},
        {"type": "tap", "x": 1},
        {"type": "tap", "x": 1, "y": 2, "text": "unexpected"},
        {"type": "tap", "x": True, "y": False},
        {"type": "tap", "x": "1", "y": "2"},
    ],
)
def test_incomplete_or_mixed_actions_cannot_send_unintended_input(client, body):
    client, service = client
    response = client.post("/api/input", headers={"X-Limulator-Token": "test-session-token"}, json=body)
    assert response.status_code == 422
    assert service.inputs == []


def test_named_requests_keep_status_frames_and_controls_on_their_target():
    class Named(Service):
        def __init__(self, name, foldable=False):
            super().__init__()
            self.name, self.foldable = name, foldable
            self.lifecycle = []

        def status(self):
            return {"name": self.name, "state": "stopped", "device": {}}

        def capture(self):
            return Frame(self.name.encode(), 4, 6, 0)

        def start(self):
            self.lifecycle.append("start")
            return {"name": self.name, "state": "booting"}

        def stop(self):
            self.lifecycle.append("stop")
            return {"name": self.name, "state": "stopped"}

        def fold(self, position):
            if not self.foldable:
                raise DeviceError("Fold posture is only available on iPhone Duo")
            return {"position": position}

    duo, mini = Named("duo", True), Named("mini")

    class Console:
        def for_device(self, name):
            if name not in {"duo", "mini"}:
                raise ValueError(f"unknown device: {name}")
            return {"duo": duo, "mini": mini}[name]

        def status(self):
            return duo.status()

    headers = {"X-Limulator-Token": "test-token"}
    with TestClient(create_app(Console(), token="test-token"), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/status").json()["name"] == "duo"
        assert client.get("/api/status?device=mini").json()["name"] == "mini"
        assert client.get("/api/screen.png?device=duo").content == b"duo"
        assert client.get("/api/screen.png?device=mini").content == b"mini"
        assert (
            client.post(
                "/api/input?device=mini",
                headers=headers,
                json={"type": "tap", "x": 1, "y": 2, "frame_id": "4x6:0"},
            ).status_code
            == 200
        )
        assert mini.inputs == [(1, 2, "4x6:0")]
        assert duo.inputs == []
        assert client.post("/api/start?device=mini", headers=headers).json()["state"] == "booting"
        assert client.post("/api/stop?device=duo", headers=headers).json()["state"] == "stopped"
        assert mini.lifecycle == ["start"]
        assert duo.lifecycle == ["stop"]
        assert client.post("/api/stop?device=missing", headers=headers).status_code == 404
        assert client.post("/api/start?device=duo").status_code == 403
        assert (
            client.post("/api/fold?device=mini", headers=headers, json={"position": "open"}).status_code
            == 409
        )
        assert (
            client.post("/api/fold?device=duo", headers=headers, json={"position": "open"}).status_code == 200
        )


@pytest.fixture
def uploads(monkeypatch):
    monkeypatch.setattr(server, "MAX_IPA_BYTES", 32, raising=False)
    monkeypatch.setattr(server, "MULTIPART_OVERHEAD_BYTES", 256, raising=False)
    monkeypatch.setattr(formparsers.MultiPartParser, "spool_max_size", 8)
    spools = []
    original = formparsers.SpooledTemporaryFile

    def spool(*args, **kwargs):
        file = original(*args, **kwargs)
        observed = {"file": file, "written": 0, "rolled": False}
        spools.append(observed)
        write = file.write

        def tracked_write(data):
            result = write(data)
            observed["written"] += result
            observed["rolled"] |= file._rolled
            return result

        file.write = tracked_write
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    service = Service()
    service.installed = []
    service.install = lambda path: service.installed.append(path.read_bytes()) or {"installed": True}
    return create_app(service, token="test-session-token"), service, spools


def upload(app, size, extra_headers=None):
    prefix = (
        b"--test-boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="application.ipa"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    chunks = [prefix, *[b"x" * min(32, size - offset) for offset in range(0, size, 32)]]
    chunks.append(b"\r\n--test-boundary--\r\n")
    consumed = []

    async def request():
        async def body():
            for chunk in chunks:
                consumed.append(chunk)
                yield chunk

        headers = {
            "X-Limulator-Token": "test-session-token",
            "Content-Type": "multipart/form-data; boundary=test-boundary",
            **(extra_headers or {}),
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            return await client.post("/api/install", headers=headers, content=body())

    return asyncio.run(request()), consumed, chunks


def test_oversized_upload_length_is_rejected_before_reading_or_spooling(uploads):
    app, service, spools = uploads
    response, consumed, _ = upload(app, 512, {"Content-Length": "1024"})
    assert response.status_code == 413
    assert consumed == []
    assert spools == []
    assert service.installed == []


@pytest.mark.parametrize("length", ["-1", "invalid"])
def test_invalid_upload_length_is_rejected_without_reading_or_spooling(uploads, length):
    app, service, spools = uploads
    response, consumed, _ = upload(app, 32, {"Content-Length": length})
    assert response.status_code == 400
    assert consumed == []
    assert spools == []
    assert service.installed == []


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_streaming_upload_limit_closes_partial_spools_without_consuming_the_rest(uploads, headers):
    app, service, spools = uploads
    response, consumed, chunks = upload(app, 512, headers)
    assert response.status_code == 413
    assert len(consumed) < len(chunks)
    assert service.installed == []
    assert spools and any(spool["rolled"] for spool in spools)
    assert all(spool["file"].closed for spool in spools)
    assert sum(spool["written"] for spool in spools) <= 32 + 256


def test_upload_request_allowance_preserves_file_limit_and_multipart_overhead(uploads):
    app, service, spools = uploads
    response, _, _ = upload(app, 32)
    assert response.status_code == 200
    assert service.installed == [b"x" * 32]
    response, _, _ = upload(app, 33)
    assert response.status_code == 413
    assert service.installed == [b"x" * 32]
    assert all(spool["file"].closed for spool in spools)


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Limulator-Token": "wrong"},
        {"Origin": "https://other.example"},
        {"Host": "other.example"},
    ],
)
def test_oversized_uploads_still_require_authorized_loopback_origin_before_body_reads(uploads, headers):
    app, service, spools = uploads
    response, consumed, _ = upload(app, 512, {"Content-Length": "1024", **headers})
    assert response.status_code == 403
    assert consumed == []
    assert spools == []
    assert service.installed == []
