"""The stub reproduces the contract's golden vectors byte-exact."""
import asyncio
import struct

import pytest

from spark_bridge.stubnode import (
    OWN_PUBKEY, StubNode, build_battery, build_channel_msg, build_room_post, build_stats, frame_out,
)

T0 = 1700000000
H = bytes.fromhex


def wire(payload: bytes) -> bytes:
    return b"<" + struct.pack("<H", len(payload)) + payload


class Client:
    def __init__(self, r, w):
        self.r, self.w = r, w
        self.pushes = []

    async def frame(self, timeout=2.0) -> bytes:
        hdr = await asyncio.wait_for(self.r.readexactly(3), timeout)
        assert hdr[0:1] == b">"
        n = hdr[1] | hdr[2] << 8
        return await asyncio.wait_for(self.r.readexactly(n), timeout)

    pushes: list

    async def call(self, payload: bytes) -> bytes:
        self.w.write(wire(payload))
        await self.w.drain()
        while True:
            f = await self.frame()
            if f[0] >= 0x80:
                self.pushes.append(f)
                continue
            return f


@pytest.fixture
async def cl(stub):
    r, w = await asyncio.open_connection("127.0.0.1", stub.port)
    yield Client(r, w)
    w.close()


# V1: host->node channel send
V1 = H("03000000f153655b726f62696e5d20726164696f20636865636b")
V4 = H("02000000f15365a1b2c3d4e5f65b726f62696e5d20686920726f6f6d")


async def test_v1_channel_send(stub, cl):
    assert await cl.call(V1) == b"\x00"
    assert stub.transmitted == ["SparkNode: [robin] radio check"]
    # the vector is what the host builds
    assert V1 == b"\x03\x00\x00" + struct.pack("<I", T0) + b"[robin] radio check"


def test_v2_channel_message():
    assert build_channel_msg("Bob: hi", T0, snr=5.0, path_len=2) == H("1114000000020000f15365426f623a206869")


def test_v3_room_post():
    assert build_room_post(H("deadbeef"), "hello room", T0) == H(
        "10000000a1b2c3d4e5f6ff0200f15365deadbeef68656c6c6f20726f6f6d")


async def test_v4_room_post_reply_and_no_echo(stub, cl):
    assert await cl.call(V4) == H("06000000000000000000")
    assert stub.room_pushed == ["[robin] hi room"]
    assert V4 == b"\x02\x00\x00" + struct.pack("<I", T0) + OWN_PUBKEY[:6] + b"[robin] hi room"
    await cl.call(b"\x01\x03      test")  # APP_START replays everything ...
    # ... but not the first time round for a host that never lost it: sync now
    reply = await cl.call(b"\x0a")
    assert reply[0] == 0x10  # replayed after APP_START (stub replays host posts too, see README)


async def test_v4_no_echo_without_app_start(stub, cl):
    await cl.call(V4)
    assert await cl.call(b"\x0a") == b"\x0a"


def test_v5_v6_v7():
    assert build_stats(0, batt=4012, uptime=86400) == H("1800ac0f80510100000000")
    assert build_stats(1, noise=-112, rssi=-95, snr=7.5, tx=120, rx=3600) == H("180190ffa11e78000000100e0000")
    assert build_battery(4012) == H("0cac0f0000000000000000")


async def test_stats_and_battery_over_wire(stub, cl):
    assert await cl.call(H("1400")[:1]) == H("0cac0f0000000000000000")
    assert await cl.call(H("3800")) == H("1800ac0f80510100000000")
    assert await cl.call(H("3801")) == H("180190ffa11e78000000100e0000")
    assert (await cl.call(H("3802")))[:2] == H("1802")
    assert await cl.call(H("3803")) == H("0106")


async def test_v8_v9_cli(stub, cl):
    cmd = H("4267657420726570656174")
    assert await cl.call(cmd) == H("1d3e206f6e")
    assert wire(cmd) == H("3c0b004267657420726570656174")
    assert frame_out(H("1d3e206f6e")) == H("3e05001d3e206f6e")


async def test_cli_refuses_ble_pin(stub, cl):
    assert (await cl.call(b"Bset ble.pin 123456"))[0] == 0x1D


async def test_handshake_frames(stub, cl):
    si = await cl.call(b"\x01\x03      test")
    assert si[0] == 5 and si[4:36] == OWN_PUBKEY and si.endswith(b"SparkNode")
    di = await cl.call(b"\x16\x03")
    assert len(di) == 82 and di[0] == 0x0D and b"SparkNode1" in di
    assert (await cl.call(b"\x05"))[0] == 9
    assert await cl.call(b"\x06" + struct.pack("<I", T0 - 5)) == b"\x01\x06"
    assert await cl.call(b"\x06" + struct.pack("<I", T0 + 5)) == b"\x00"


async def test_error_codes(stub, cl):
    assert await cl.call(b"\x63") == b"\x01\x01"  # unknown command
    assert await cl.call(b"\x20" + bytes(10)) == b"\x01\x01"  # SET_CHANNEL
    assert await cl.call(b"\x1f\x01") == b"\x01\x02"  # GET_CHANNEL other idx
    ci = await cl.call(b"\x1f\x00")
    assert ci[0] == 0x12 and ci[2:8] == b"Public"
    assert await cl.call(b"\x03\x01\x00" + struct.pack("<I", T0) + b"x") == b"\x01\x01"  # txt_type
    assert await cl.call(b"\x03\x00\x05" + struct.pack("<I", T0) + b"x") == b"\x01\x02"  # idx
    assert await cl.call(b"\x02\x00\x00" + struct.pack("<I", T0) + bytes(6) + b"x") == b"\x01\x02"  # other dest
    assert await cl.call(b"\x02\x01\x00" + struct.pack("<I", T0) + OWN_PUBKEY[:6] + b"x") == b"\x01\x01"


async def test_replay_after_app_start(stub, cl):
    stub.inject_room_post("deadbeef", "one", T0 - 30)
    stub.inject_room_post("deadbeef", "two", T0 - 20)
    assert [await cl.frame(), await cl.frame()] == [b"\x83", b"\x83"]
    got = []
    while (f := await cl.call(b"\x0a"))[0] == 0x10:
        got.append(f[-3:])
    assert got == [b"one", b"two"] and f == b"\x0a"
    # a new APP_START offers them again, followed by MSG_WAITING
    await cl.call(b"\x01\x03      test")
    assert await cl.frame() == b"\x83"
    assert (await cl.call(b"\x0a"))[-3:] == b"one"


async def test_ring_overflow_keeps_newest_32(stub, cl):
    for i in range(40):
        stub.inject_channel("Bob", f"m{i}")
    got = []
    while (f := await cl.call(b"\x0a"))[0] == 0x11:
        got.append(f[11:].decode())
    assert len(got) == 32 and got[0] == "Bob: m8" and got[-1] == "Bob: m39"


async def test_contacts_and_advert_push(stub, cl):
    stub.inject_advert("Alice")
    push = await cl.frame()
    assert push[0] == 0x8A and len(push) == 148
    cl.w.write(wire(b"\x04"))
    frames = [await cl.frame() for _ in range(3)]
    assert [f[0] for f in frames] == [2, 3, 4]
