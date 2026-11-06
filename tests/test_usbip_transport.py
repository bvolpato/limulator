"""Exercise the pinned external backend's USB/IP stream when it is installed."""

import asyncio
import errno
import importlib.util
import os
import struct
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import pytest

BACKEND = Path(
    os.environ.get("LIMULATOR_BACKEND", Path.home() / ".local/share/limulator/backends/vphone-linux")
)
EXPORTER = BACKEND / "tools/vusb-usbip.py"
if not EXPORTER.is_file():
    pytest.skip("external vphone-linux backend is not installed", allow_module_level=True)

spec = importlib.util.spec_from_file_location("limulator_test_vusb_usbip", EXPORTER)
assert spec is not None and spec.loader is not None
usbip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usbip)


class USB:
    def __init__(self):
        self.started = asyncio.Event()
        self.payloads = []
        self.aborted = []

    async def transfer(self, ep, direction_in, payload=b"", max_length=None):
        if direction_in:
            self.started.set()
            await asyncio.Event().wait()
        self.payloads.append(payload)
        return b""

    async def abort(self, ep, direction_in):
        self.aborted.append((ep, direction_in))


def submit(seqnum, direction, length):
    return struct.pack("!IIIIIIiiii8s", 1, seqnum, 0x10001, direction, 1, 0, length, 0, 0, 0, bytes(8))


async def fragmented_write(writer, data):
    sizes = (1, 3, 7, 2, 19, 5)
    offset = 0
    index = 0
    while offset < len(data):
        size = sizes[index % len(sizes)]
        writer.write(data[offset : offset + size])
        await writer.drain()
        await asyncio.sleep(0.001)
        offset += size
        index += 1


async def unlink_then_submit(active):
    usb = USB()
    bridge = usbip.USBIPBridge(usb)
    bridge.device_record = bytes(312)
    server = await asyncio.start_server(bridge.handle_connection, "127.0.0.1", 0)
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(struct.pack("!HHI32s", 0x0111, 0x8003, 0, b"1-1"))
        await writer.drain()
        assert await reader.readexactly(8) == struct.pack("!HHI", 0x0111, 3, 0)
        await reader.readexactly(312)

        if active:
            writer.write(submit(9, 1, 8))
            await writer.drain()
            await usb.started.wait()

        payload = b"payload after unlink"
        unlink = struct.pack("!IIIIII24x", 2, 20, 0x10001, 0, 0, 9)
        await fragmented_write(writer, unlink + submit(21, 0, len(payload)) + payload)

        reply = await reader.readexactly(48)
        expected_status = -errno.ECONNRESET if active else 0
        assert reply == struct.pack("!IIIIIi24x", 4, 20, 0, 0, 0, expected_status)
        # The next frame must complete the new SUBMIT, never the canceled one.
        fields = struct.unpack("!IIIIIiiiii8s", await reader.readexactly(48))
        assert fields[0:2] == (3, 21)
        assert fields[5:7] == (0, len(payload))
        assert usb.payloads == [payload]
        assert usb.aborted == ([(1, True)] if active else [])
    finally:
        if writer is not None:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("active", [False, True], ids=["completed-target", "pending-target"])
def test_fragmented_unlink_keeps_next_submit_aligned(active):
    async def bounded():
        async with asyncio.timeout(3):
            await unlink_then_submit(active)

    asyncio.run(bounded())


@pytest.fixture
def short_deadline_usbip(monkeypatch):
    monkeypatch.setenv("VUSB_URB_TIMEOUT", "0.02")
    spec = importlib.util.spec_from_file_location("limulator_test_vusb_deadlines", EXPORTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CTRL_TIMEOUT = 0.02
    return module


class Endpoint:
    """A socket peer that holds transfers until the test supplies guest data."""

    def __init__(self, module):
        self.module = module
        self.requests = asyncio.Queue()
        self.writer = None

    async def handle(self, reader, writer):
        self.writer = writer
        try:
            while True:
                (length,) = struct.unpack("<I", await reader.readexactly(4))
                ep, kind = await reader.readexactly(2)
                payload = await reader.readexactly(length)
                await self.requests.put((ep, kind, payload))
                if kind == self.module.MSG_ABORT:
                    await self.respond(self.module.CODE_ABORT, ep)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def respond(self, code, ep, data=b""):
        self.writer.write(struct.pack("<IBB", len(data) + 2, code, ep) + data)
        await self.writer.drain()

    async def request(self, ep, kind=1):
        while True:
            request = await self.requests.get()
            if request[:2] == (ep, kind):
                return request[2]


@asynccontextmanager
async def socket_bridge(module):
    endpoint = Endpoint(module)
    guest_server = await asyncio.start_server(endpoint.handle, "127.0.0.1", 0)
    guest_reader, guest_writer = await asyncio.open_connection(
        "127.0.0.1", guest_server.sockets[0].getsockname()[1]
    )
    usb = module.VUSB(guest_reader, guest_writer)
    pump = asyncio.create_task(usb.run())
    bridge = module.USBIPBridge(usb)
    bridge.device_record = bytes(312)
    server = await asyncio.start_server(bridge.handle_connection, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        writer.write(struct.pack("!HHI32s", 0x0111, 0x8003, 0, b"1-1"))
        await writer.drain()
        await reader.readexactly(320)
        yield usb, endpoint, reader, writer
    finally:
        writer.close()
        guest_writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()
            await guest_writer.wait_closed()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        server.close()
        guest_server.close()
        await server.wait_closed()
        await guest_server.wait_closed()


async def read_reply(reader):
    fields = struct.unpack("!IIIIIiiiii8s", await reader.readexactly(48))
    data = await reader.readexactly(fields[6]) if fields[3] == 1 and fields[6] else b""
    return fields, data


@pytest.mark.parametrize(
    "completion", ["data", "unlink", "driver-reset", "disconnect", "malformed-short", "malformed-long"]
)
def test_idle_bulk_in_waits_for_data_or_device_lifecycle(short_deadline_usbip, completion):
    module = short_deadline_usbip

    async def run():
        async with asyncio.timeout(1), socket_bridge(module) as (usb, endpoint, reader, writer):
            writer.write(submit(10, 1, 8))
            await writer.drain()
            await endpoint.request(0x81)
            # Host usbmuxd submits these reads with no deadline. A quiet guest is valid.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readexactly(48), 0.06)

            if completion == "data":
                await endpoint.respond(module.CODE_DATA, 0x81, b"ready")
                fields, data = await read_reply(reader)
                assert fields[1] == 10 and fields[5:7] == (0, 5) and data == b"ready"
            elif completion == "unlink":
                await fragmented_write(writer, struct.pack("!IIIIII24x", 2, 20, 0x10001, 0, 0, 10))
                fields, _ = await read_reply(reader)
                assert fields[:2] == (4, 20) and fields[5] == -errno.ECONNRESET
                writer.write(submit(21, 1, 8))
                await writer.drain()
                await endpoint.request(0x81)
                await endpoint.respond(module.CODE_DATA, 0x81, b"next")
                fields, data = await read_reply(reader)
                assert fields[1] == 21 and fields[5:7] == (0, 4) and data == b"next"
            else:
                if completion == "driver-reset":
                    await endpoint.respond(module.CODE_DRIVER_RESET, 0)
                elif completion.startswith("malformed-"):
                    length = 1 if completion == "malformed-short" else module.VUSB_MSG_MAX + 1
                    endpoint.writer.write(struct.pack("<I", length))
                    await endpoint.writer.drain()
                else:
                    endpoint.writer.close()
                fields, _ = await read_reply(reader)
                assert fields[1] == 10 and fields[5:7] == (-errno.ENODEV, 0)
                assert usb.detached.is_set()
                if completion == "driver-reset":
                    # A boot-stage reset unplugs the old device but keeps the pump alive.
                    usb.detached.clear()
                    writer.write(submit(22, 1, 8))
                    await writer.drain()
                    await endpoint.request(0x81)
                    await endpoint.respond(module.CODE_DATA, 0x81, b"stage")
                    fields, data = await read_reply(reader)
                    assert fields[1] == 22 and fields[5:7] == (0, 5) and data == b"stage"

    asyncio.run(run())


@pytest.mark.parametrize("control", [False, True], ids=["bulk-out", "control"])
def test_finite_transfer_timeout_reports_etimedout(short_deadline_usbip, control):
    async def run():
        async with asyncio.timeout(1), socket_bridge(short_deadline_usbip) as (_, endpoint, reader, writer):
            if control:
                setup = struct.pack("<BBHHH", 0x40, 0, 0, 0, 3)
                request = struct.pack("!IIIIIIiiii8s", 1, 30, 0x10001, 0, 0, 0, 3, 0, 0, 0, setup)
            else:
                request = submit(30, 0, 3)
            writer.write(request + b"cmd")
            await writer.drain()
            await endpoint.request(0 if control else 1)
            fields, _ = await read_reply(reader)
            assert fields[1] == 30 and fields[5:7] == (-errno.ETIMEDOUT, 0)

    asyncio.run(run())


def test_reset_handshake_keeps_its_deadline(short_deadline_usbip):
    async def run():
        async with asyncio.timeout(1), socket_bridge(short_deadline_usbip) as (usb, _, _, _):
            with pytest.raises(asyncio.TimeoutError):
                await usb.reset(timeout=0.02)
            assert not usb.reset_pending

    asyncio.run(run())
