"""Outcome tests: a dead local session is rebuilt after repeated timeouts.

Only the network boundary is faked: asyncio.open_connection is replaced by
an in-memory socket to a fake vacuum that speaks the Tuya 3.3 wire format.
The integration is driven through TuyaDevice.async_get().
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.robovac.tuyalocalapi import (
    MAGIC_SUFFIX_BYTES,
    SESSION_REBUILD_TIMEOUTS,
    Message,
    ResponseTimeoutException,
    TuyaCipher,
    TuyaDevice,
)

LOCAL_KEY = "0123456789abcdef"


class _Socket:
    """In-memory stand-in for one TCP connection to the vacuum."""

    def __init__(self, vacuum: "FakeVacuum", index: int) -> None:
        self.vacuum = vacuum
        self.index = index
        self.reader = asyncio.StreamReader()
        self.buffer = b""
        self.closed = False

    # StreamWriter API used by the integration
    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionResetError("socket closed")
        self.buffer += data
        while MAGIC_SUFFIX_BYTES in self.buffer:
            frame, _, self.buffer = self.buffer.partition(MAGIC_SUFFIX_BYTES)
            self.vacuum.receive(self, frame + MAGIC_SUFFIX_BYTES)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    def is_closing(self) -> bool:
        return self.closed

    async def wait_closed(self) -> None:
        return None


class FakeVacuum:
    """Protocol 3.3 device whose first sessions are wedged (never answer)."""

    def __init__(self, silent_connections: int) -> None:
        self.silent_connections = silent_connections
        self.connections = 0
        self.codec = TuyaDevice.__new__(TuyaDevice)
        self.codec.version = (3, 3)
        self.codec.cipher = TuyaCipher(LOCAL_KEY, (3, 3))

    async def open_connection(self, host, port, *args, **kwargs):
        self.connections += 1
        sock = _Socket(self, self.connections)
        return sock.reader, sock

    def receive(self, sock: _Socket, frame: bytes) -> None:
        if sock.index <= self.silent_connections:
            return  # wedged session: swallow requests
        request = Message.from_bytes(self.codec, frame, self.codec.cipher)
        if request.command != Message.GET_COMMAND:
            return
        reply = Message(
            Message.GET_COMMAND,
            json.dumps({"dps": {"15": "Running"}}).encode(),
            sequence=request.sequence,
            encrypt=True,
            device=self.codec,
            expect_response=False,
        )
        sock.reader.feed_data(reply.to_bytes())


async def _start(fake: FakeVacuum):
    patcher = patch(
        "custom_components.robovac.tuyalocalapi.asyncio.open_connection",
        side_effect=fake.open_connection,
    )
    patcher.start()
    device = TuyaDevice(
        model_details=SimpleNamespace(commands={}),
        device_id="fake-device",
        host="192.0.2.10",
        timeout=0.5,
        ping_interval=3600,
        update_entity_state=AsyncMock(),
        local_key=LOCAL_KEY,
        version=(3, 3),
    )
    return patcher, device


async def _stop(patcher, device) -> None:
    await device.async_disable()
    patcher.stop()
    # The device schedules its own queue/ping loops; stop them for teardown.
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_session_is_rebuilt_after_consecutive_timeouts_and_recovers() -> None:
    """After N timeouts a brand-new session is opened and status flows again."""
    fake = FakeVacuum(silent_connections=SESSION_REBUILD_TIMEOUTS)
    patcher, device = await _start(fake)
    try:
        for _ in range(SESSION_REBUILD_TIMEOUTS):
            with pytest.raises(ResponseTimeoutException):
                await device.async_get()

        # The rebuild has already opened a fresh connection to the device.
        await asyncio.sleep(0.1)
        assert fake.connections == SESSION_REBUILD_TIMEOUTS + 1

        await device.async_get()
        assert device.state.get("15") == "Running"

        # Further polls stay on the healthy session.
        await device.async_get()
        assert fake.connections == SESSION_REBUILD_TIMEOUTS + 1
    finally:
        await _stop(patcher, device)


@pytest.mark.asyncio
async def test_successful_responses_reset_the_timeout_count() -> None:
    """Isolated timeouts separated by successes never trigger a rebuild."""
    fake = FakeVacuum(silent_connections=0)
    patcher, device = await _start(fake)
    try:
        for _ in range(SESSION_REBUILD_TIMEOUTS + 2):
            await device.async_get()
        assert device.state.get("15") == "Running"
        assert fake.connections == 1
    finally:
        await _stop(patcher, device)
