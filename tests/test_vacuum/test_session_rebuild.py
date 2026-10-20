"""Outcome tests: a dead local session is rebuilt after repeated timeouts.

Only the network boundary is faked: ``asyncio.open_connection`` is replaced by
an in-memory socket to a fake vacuum that speaks the Tuya wire format (3.3,
or 3.5 including the SESS_KEY_NEG handshake). The integration is driven through
``TuyaDevice`` public methods and observed through what the fake device sees
and through ``TuyaDevice.state``.
"""

import asyncio
import json
import logging
import os
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import hmac as crypto_hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256

from custom_components.robovac import tuyalocalapi
from custom_components.robovac.tuyalocalapi import (
    MAGIC_PREFIX,
    MAGIC_PREFIX_35,
    MAGIC_SUFFIX_35,
    Message,
    ResponseTimeoutException,
    TuyaCipher,
    TuyaDevice,
)

# Looked up defensively so this module still imports on code without the fix
# and the tests there fail on behaviour rather than on an ImportError.
SESSION_REBUILD_TIMEOUTS = getattr(tuyalocalapi, "SESSION_REBUILD_TIMEOUTS", 3)

LOCAL_KEY = "0123456789abcdef"
# A realistic L35 Hybrid (T2194) status poll.
STATUS_DPS = {"1": True, "2": False, "5": "auto", "15": "Charging", "104": 87}
REBUILD_LOG = "rebuilding local session"


class _Socket:
    """In-memory stand-in for one TCP connection to the vacuum."""

    def __init__(self, vacuum, index):
        """Bind the socket to a fake vacuum with a 1-based connection index."""
        self.vacuum = vacuum
        self.index = index
        self.reader = asyncio.StreamReader()
        self.buffer = b""
        self.closed = False

    def write(self, data):
        """Buffer written bytes and hand complete frames to the fake vacuum."""
        if self.closed:
            raise ConnectionResetError("socket closed")
        self.buffer += data
        while len(self.buffer) >= 18:
            prefix = struct.unpack_from(">I", self.buffer)[0]
            if prefix == MAGIC_PREFIX_35:
                total = 18 + struct.unpack_from(">I", self.buffer, 14)[0] + 4
            elif prefix == MAGIC_PREFIX:
                total = 16 + struct.unpack_from(">I", self.buffer, 12)[0]
            else:
                raise AssertionError("unexpected frame prefix")
            if len(self.buffer) < total:
                break
            frame, self.buffer = self.buffer[:total], self.buffer[total:]
            self.vacuum.receive(self, frame)

    async def drain(self):
        """Match the StreamWriter API."""
        return None

    def close(self):
        """Close the socket; its reader sees EOF like a real socket would."""
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    def is_closing(self):
        """Match the StreamWriter API."""
        return self.closed

    async def wait_closed(self):
        """Match the StreamWriter API."""
        return None


class FakeVacuum33:
    """Protocol 3.3 vacuum that can be wedged (silently drops every request).

    A wedged device models the state seen after sleep/standby: the TCP
    connection is accepted but nothing is ever answered.
    """

    version = (3, 3)

    def __init__(self, wedged=False):
        """Create the fake, optionally starting wedged."""
        self.wedged = wedged
        self.sockets = []
        self.requests = 0
        self.codec = TuyaDevice.__new__(TuyaDevice)
        self.codec.version = self.version
        self.codec.cipher = TuyaCipher(LOCAL_KEY, self.version)

    @property
    def connections(self):
        """Return how many TCP connections the integration opened."""
        return len(self.sockets)

    @property
    def open_sockets(self):
        """Return the connections the integration has not closed."""
        return [s for s in self.sockets if not s.closed]

    async def open_connection(self, host, port, *args, **kwargs):
        """Accept a new connection."""
        sock = _Socket(self, len(self.sockets) + 1)
        self.sockets.append(sock)
        return sock.reader, sock

    def receive(self, sock, frame):
        """Answer GET requests unless wedged."""
        request = Message.from_bytes(self.codec, frame, self.codec.cipher)
        if request.command != Message.GET_COMMAND:
            return
        self.requests += 1
        if self.wedged:
            return
        reply = Message(
            Message.GET_COMMAND,
            json.dumps({"devId": "fake-device", "dps": STATUS_DPS}).encode(),
            sequence=request.sequence,
            encrypt=True,
            device=self.codec,
            expect_response=False,
        )
        sock.reader.feed_data(reply.to_bytes())


def _hmac(key, data):
    h = crypto_hmac.HMAC(key, SHA256())
    h.update(data)
    return h.finalize()


class FakeVacuum35(FakeVacuum33):
    """Protocol 3.5 vacuum that performs the SESS_KEY_NEG handshake.

    Every connection gets a fresh device nonce, so every successful
    negotiation yields a different session key. The fake records the keys
    it derived and can push a status update encrypted with the current one.
    """

    version = (3, 5)

    def __init__(self, wedged=False):
        """Create the fake."""
        super().__init__(wedged)
        self.handshakes = []  # (client_nonce, session_key) per completed handshake
        self._pending = {}

    def receive(self, sock, frame):
        """Run the handshake; ignore everything else."""
        command = struct.unpack_from(">I", frame, 10)[0]
        real_key = LOCAL_KEY.encode()
        if command not in (Message.SESS_KEY_NEG_START, Message.SESS_KEY_NEG_FINISH):
            return
        plain = AESGCM(real_key).decrypt(frame[18:30], frame[30:-4], frame[4:18])
        if command == Message.SESS_KEY_NEG_START:
            if self.wedged:
                return
            client_nonce = plain
            device_nonce = os.urandom(16)
            self._pending[sock.index] = (client_nonce, device_nonce)
            payload = b"\x00\x00\x00\x00" + device_nonce + _hmac(real_key, client_nonce)
            header = struct.pack(
                ">IBBIII", MAGIC_PREFIX_35, 0, 0, 1, Message.SESS_KEY_NEG_RESP,
                12 + len(payload) + 16,
            )
            iv = os.urandom(12)
            sealed = AESGCM(real_key).encrypt(iv, payload, header[4:])
            sock.reader.feed_data(
                header + iv + sealed + struct.pack(">I", MAGIC_SUFFIX_35)
            )
        else:
            client_nonce, device_nonce = self._pending.pop(sock.index)
            assert plain == _hmac(real_key, device_nonce)
            xored = bytes(a ^ b for a, b in zip(client_nonce, device_nonce))
            key = AESGCM(real_key).encrypt(client_nonce[:12], xored, None)[:16]
            self.handshakes.append((client_nonce, key))

    def push_status(self, dps):
        """Push a gratuitous status update on the newest connection."""
        self.codec.cipher = TuyaCipher(LOCAL_KEY, self.version)
        self.codec.cipher.set_session_key(self.handshakes[-1][1])
        message = Message(
            Message.GRATUITOUS_UPDATE,
            json.dumps({"protocol": 4, "data": {"dps": dps}}).encode(),
            sequence=7,
            encrypt=True,
            device=self.codec,
            expect_response=False,
        )
        self.sockets[-1].reader.feed_data(message.to_bytes())


@pytest.fixture
async def make_device():
    """Build TuyaDevices wired to a fake vacuum; tear everything down after."""
    created = []

    async def _make(fake):
        patcher = patch(
            "custom_components.robovac.tuyalocalapi.asyncio.open_connection",
            side_effect=fake.open_connection,
        )
        patcher.start()
        device = TuyaDevice(
            model_details=SimpleNamespace(commands={}),
            device_id="fake-device",
            host="192.0.2.10",
            timeout=0.2,
            ping_interval=3600,
            update_entity_state=AsyncMock(),
            local_key=LOCAL_KEY,
            version=fake.version,
        )
        created.append((patcher, device))
        return device

    yield _make

    for patcher, device in created:
        await device.async_disable()
        patcher.stop()
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _time_out(device, times):
    for _ in range(times):
        with pytest.raises(ResponseTimeoutException):
            await device.async_get()
    # Let reader loops of discarded sockets observe their EOF.
    await asyncio.sleep(0.05)


async def _until(predicate, timeout=1.0):
    """Wait (bounded) for the event loop to deliver queued device frames."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


def _rebuilds(caplog):
    return sum(REBUILD_LOG in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("timeouts", range(1, SESSION_REBUILD_TIMEOUTS))
async def test_timeouts_below_threshold_reconnect_without_rebuild(
    make_device, caplog, timeouts
):
    """Fewer than the threshold: plain reconnect, no rebuild, and polling recovers."""
    caplog.set_level(logging.WARNING)
    fake = FakeVacuum33(wedged=True)
    device = await make_device(fake)

    await _time_out(device, timeouts)
    assert _rebuilds(caplog) == 0

    fake.wedged = False
    await device.async_get()
    assert device.state == STATUS_DPS
    # One connection per timed-out poll plus the healthy one; nothing leaked.
    assert fake.connections == timeouts + 1
    assert fake.open_sockets == [fake.sockets[-1]]


async def test_rebuild_at_threshold_then_recovers_with_real_status(make_device, caplog):
    """At the threshold the session is rebuilt once and status flows again."""
    caplog.set_level(logging.WARNING)
    fake = FakeVacuum33(wedged=True)
    device = await make_device(fake)

    await _time_out(device, SESSION_REBUILD_TIMEOUTS)
    assert _rebuilds(caplog) == 1
    # The rebuild already opened the fresh connection.
    assert fake.connections == SESSION_REBUILD_TIMEOUTS + 1
    assert fake.open_sockets == [fake.sockets[-1]]

    fake.wedged = False
    await device.async_get()
    await device.async_get()
    assert device.state == STATUS_DPS
    assert fake.connections == SESSION_REBUILD_TIMEOUTS + 1


async def test_stale_reader_close_does_not_kill_new_connection(make_device):
    """EOF on a discarded socket must not tear down the connection that replaced it."""
    fake = FakeVacuum33(wedged=True)
    device = await make_device(fake)

    with pytest.raises(ResponseTimeoutException):
        await device.async_get()
    # Poll again straight away, before the discarded socket's reader loop
    # has processed its EOF - the order seen when HA polls right after a timeout.
    fake.wedged = False
    await device.async_get()
    healthy = fake.sockets[-1]
    # Every earlier socket is closed; their readers have hit EOF by now.
    await asyncio.sleep(0.05)
    assert not healthy.closed
    # The wedged connection plus exactly one replacement: the stale EOF did
    # not drop the replacement and force yet another reconnect.
    assert fake.connections == 2

    for _ in range(3):
        await device.async_get()
    assert device.state == STATUS_DPS
    assert fake.sockets[-1] is healthy


async def test_repeated_rebuild_cycles_each_recover(make_device, caplog):
    """Wedge, rebuild and recover three times in a row on the same device."""
    caplog.set_level(logging.WARNING)
    fake = FakeVacuum33()
    device = await make_device(fake)

    for cycle in range(1, 4):
        fake.wedged = True
        await _time_out(device, SESSION_REBUILD_TIMEOUTS)
        assert _rebuilds(caplog) == cycle
        fake.wedged = False
        await device.async_get()
        assert device.state == STATUS_DPS
        assert len(fake.open_sockets) == 1


async def test_concurrent_polls_while_wedged_fail_cleanly_then_recover(
    make_device, caplog
):
    """Overlapping polls during a rebuild only raise ResponseTimeoutException."""
    caplog.set_level(logging.WARNING)
    fake = FakeVacuum33(wedged=True)
    device = await make_device(fake)

    results = await asyncio.wait_for(
        asyncio.gather(
            *(device.async_get() for _ in range(SESSION_REBUILD_TIMEOUTS + 1)),
            return_exceptions=True,
        ),
        timeout=5,
    )
    assert [type(r) for r in results] == [ResponseTimeoutException] * len(results)
    assert _rebuilds(caplog) >= 1

    # Requests still queued from the wedged period are delivered on the
    # rebuilt session once the device answers again.
    fake.wedged = False
    assert await _until(lambda: device.state == STATUS_DPS, timeout=2)
    assert await _until(lambda: len(fake.open_sockets) <= 1, timeout=2)


async def test_protocol_35_rebuild_negotiates_fresh_session_key(make_device):
    """A 3.5 rebuild runs a new handshake and the new key decrypts device pushes."""
    fake = FakeVacuum35()
    device = await make_device(fake)

    await device.async_get()  # 3.5 polling connects and negotiates
    assert len(fake.handshakes) == 1

    await device.async_rebuild_session()
    assert fake.connections == 2
    assert fake.sockets[0].closed and not fake.sockets[1].closed
    assert len(fake.handshakes) == 2
    (nonce1, key1), (nonce2, key2) = fake.handshakes
    assert nonce1 != nonce2 and key1 != key2

    fake.push_status({"15": "Running", "104": 64})
    assert await _until(lambda: device.state.get("15") == "Running")
    assert device.state["15"] == "Running"
    assert device.state["104"] == 64


async def test_protocol_35_normal_operation_negotiates_once(make_device):
    """Regression guard: healthy 3.5 polling keeps one session and one key."""
    fake = FakeVacuum35()
    device = await make_device(fake)

    for _ in range(5):
        await device.async_get()
    fake.push_status({"15": "Running"})
    assert await _until(lambda: device.state.get("15") == "Running")
    assert device.state["15"] == "Running"
    assert fake.connections == 1
    assert len(fake.handshakes) == 1


async def test_normal_polling_never_rebuilds(make_device, caplog):
    """Regression guard: healthy 3.3 polling uses one connection and never rebuilds."""
    caplog.set_level(logging.WARNING)
    fake = FakeVacuum33()
    device = await make_device(fake)

    for _ in range(SESSION_REBUILD_TIMEOUTS * 3):
        await device.async_get()
    assert device.state == STATUS_DPS
    assert fake.connections == 1
    assert fake.requests == SESSION_REBUILD_TIMEOUTS * 3
    assert _rebuilds(caplog) == 0
