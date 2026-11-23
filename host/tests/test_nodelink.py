"""Real `meshcore` library against the stub node over TCP."""
import asyncio

import pytest

from spark_bridge.config import NodeConfig
from spark_bridge.nodelink import ChannelMsg, LinkError, LinkGaveUp, NodeLink, RoomPost

T0 = 1700000000
H = bytes.fromhex


class Rec:
    def __init__(self):
        self.up = self.down = 0
        self.posts: list[RoomPost] = []
        self.chans: list[ChannelMsg] = []
        self.adverts: list[tuple] = []
        self.polls: list[dict] = []

    async def on_link_up(self): self.up += 1
    async def on_link_down(self): self.down += 1
    async def on_room_post(self, p): self.posts.append(p)
    async def on_channel_msg(self, m): self.chans.append(m)
    async def on_advert(self, pk, name, t): self.adverts.append((pk[:8], name, t))
    async def on_poll(self, s): self.polls.append(s)


def mklink(stub, rec=None, **kw) -> NodeLink:
    cfg = NodeConfig(transport="tcp", tcp_port=stub.port, command_timeout=3, backoff_start=0.05,
                     backoff_max=0.2, poll_seconds=kw.pop("poll_seconds", 5))
    return NodeLink(cfg, rec, clock=lambda: T0, **kw)


async def wait_for(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_handshake(stub):
    stub.contacts.append((bytes([9]) * 4 + bytes(28), "Alice", 1))
    rec = Rec()
    link = mklink(stub, rec)
    await link.connect()
    try:
        assert link.node_name == "SparkNode" and link.version == "v1.18.0-SpArkNode1.3"
        assert link.supports_sender
        assert link.own_prefix == H("a1b2c3d4e5f6") and link.own_author == "a1b2c3d4"
        codes = [f[0] for f in stub.received]
        assert codes[:3] == [1, 1, 22]  # lib APP_START, ours, DEVICE_QUERY and 6 in codes and 4 in codes
        assert stub.received[codes.index(6)] == b"\x06" + (T0).to_bytes(4, "little")
        assert rec.adverts == [("09090909", "Alice", 1)]
    finally:
        await link.close()


async def test_commands_golden_bytes(stub):
    link = mklink(stub)
    await link.connect()
    try:
        assert await link.battery() == 4012
        st = await link.stats()
        assert st["battery_mv"] == 4012 and st["uptime_secs"] == 86400
        assert st["noise_floor"] == -112 and st["last_snr"] == 7.5
        assert (await link.packet_stats())["recv"] == 10

        await link.send_public("[robin] radio check")  # V1
        assert H("03000000f153655b726f62696e5d20726164696f20636865636b") in stub.received
        assert stub.transmitted == ["SparkNode: [robin] radio check"]

        await link.send_public("Hallo", sender="BadPirate")  # V10 (SparkNode1.2 sender name)
        assert H("03000000f1536548616c6c6f00426164506972617465") in stub.received
        assert stub.transmitted[-1] == "BadPirate: Hallo"

        await link.post_room("[robin] hi room")  # V4, no echo
        assert H("02000000f15365a1b2c3d4e5f65b726f62696e5d20686920726f6f6d") in stub.received
        assert await link.drain() == 0

        assert await link.cli("get repeat") == "> on"  # V8
        assert H("4267657420726570656174") in stub.received
        assert await link.cli("set ble.pin 123456") == "ERR: not allowed here"
    finally:
        await link.close()


async def test_receive_via_push_and_drain(stub):
    rec = Rec()
    link = mklink(stub, rec)
    task = asyncio.create_task(link.run())
    try:
        await wait_for(lambda: rec.up == 1)
        stub.inject_channel("Bob", "hi", ts=T0)
        stub.inject_room_post("deadbeef", "hello room", ts=T0)
        await wait_for(lambda: rec.chans and rec.posts)
        assert rec.chans[0].text == "Bob: hi" and rec.chans[0].ts == T0
        assert rec.posts[0] == RoomPost(T0, "deadbeef", "hello room")
        stub.inject_advert("Carol")
        await wait_for(lambda: any(a[1] == "Carol" for a in rec.adverts))
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_reconnect_and_replay(stub):
    rec = Rec()
    stub.inject_room_post("deadbeef", "old", ts=T0 - 5)
    link = mklink(stub, rec)
    task = asyncio.create_task(link.run())
    try:
        await wait_for(lambda: rec.up == 1)
        await link.drain()
        assert [p.text for p in rec.posts] == ["old"]
        await stub.drop_connections()
        await wait_for(lambda: rec.down == 1 and rec.up == 2)
        await link.drain()  # APP_START reset the cursor: "old" is offered again
        assert [p.text for p in rec.posts] == ["old", "old"]
        assert link.sessions == 2
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_node_unreachable_backs_off_then_recovers(stub):
    port = stub.port
    await stub.stop()
    rec = Rec()
    cfg = NodeConfig(transport="tcp", tcp_port=port, command_timeout=1, backoff_start=0.05, backoff_max=0.1)
    link = NodeLink(cfg, rec, clock=lambda: T0)
    task = asyncio.create_task(link.run())
    await asyncio.sleep(0.3)
    assert rec.up == 0
    await stub.start(port=port)
    try:
        await wait_for(lambda: rec.up == 1)
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_silent_node_is_torn_down_after_failed_polls(stub):
    rec = Rec()
    link = mklink(stub, rec, poll_seconds=0.2)
    task = asyncio.create_task(link.run())
    try:
        await wait_for(lambda: rec.up == 1)
        stub.refuse_frames = True  # node stops answering, connection stays open
        await wait_for(lambda: rec.down >= 1, timeout=20)
        stub.refuse_frames = False
        await wait_for(lambda: rec.up >= 2, timeout=20)
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_commands_fail_fast_when_down(stub):
    link = mklink(stub)
    with pytest.raises(LinkError):
        await link.battery()


# ---- polling survives reconnects and push traffic ---------------------------------------------------
async def test_battery_polling_continues_after_reconnect(stub):
    rec = Rec()
    link = mklink(stub, rec, poll_seconds=0.2)
    task = asyncio.create_task(link.run())
    try:
        await wait_for(lambda: len(rec.polls) >= 2)
        await stub.drop_connections()
        await wait_for(lambda: rec.down == 1 and rec.up == 2)
        n = len(rec.polls)
        await wait_for(lambda: len(rec.polls) >= n + 3)  # polls keep coming on the new session
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_polling_not_starved_by_message_pushes(stub):
    rec = Rec()
    link = mklink(stub, rec, poll_seconds=0.3)
    task = asyncio.create_task(link.run())

    async def push():
        while True:
            link._waiting.set()  # MESSAGES_WAITING more often than the poll interval
            await asyncio.sleep(0.05)

    pusher = asyncio.create_task(push())
    try:
        await wait_for(lambda: len(rec.polls) >= 3, timeout=5)
    finally:
        pusher.cancel()
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_failing_on_poll_handler_does_not_stop_polling(stub):
    rec = Rec()
    calls = []

    async def bad(sample):
        calls.append(sample)
        raise KeyError("boom")

    rec.on_poll = bad
    link = mklink(stub, rec, poll_seconds=0.15)
    task = asyncio.create_task(link.run())
    try:
        await wait_for(lambda: len(calls) >= 3)
        assert rec.down == 0
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


# ---- stale BLE connection / reconnect hardening -----------------------------------------------------
class FakeBleak:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


class FakeMC:
    def __init__(self, fail=False):
        self.fail, self.disconnected = fail, 0

    async def disconnect(self):
        self.disconnected += 1
        if self.fail:
            raise RuntimeError("lib lost track of its client")


async def test_close_disconnects_the_bleak_client_even_if_lib_failed():
    link = NodeLink(NodeConfig(transport="ble"))
    mc, bleak = FakeMC(fail=True), FakeBleak()
    link.mc, link._bleak, link.connected = mc, bleak, True
    await link.close()
    assert mc.disconnected == 1 and bleak.disconnects == 1
    assert link.mc is None and link._bleak is None and not link.connected


class FakeBleDevice:
    name, address = "MeshCore-SparkNode", "B233FC9C-F4B1-0E51-E2B2-FACC6B3DB8EA"


async def test_ble_uses_os_connected_peripheral_after_failed_scans(monkeypatch):
    calls = []

    async def fake_create_ble(**kw):
        calls.append(kw)
        return None if len(calls) < 4 else FakeMC()

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", fake_create_ble)
    link = NodeLink(NodeConfig(transport="ble", ble_direct_after=3))
    looked = []

    async def finder():
        looked.append(1)
        return FakeBleDevice()

    link._find_connected_ble = finder
    for _ in range(3):  # three scans that find nothing
        with pytest.raises(LinkError):
            await link._create()
        link._fail_streak += 1
    assert not looked and all("device" not in c for c in calls)
    await link._create()  # fourth attempt goes by the OS-connected device, no scan
    assert looked and isinstance(calls[-1]["device"], FakeBleDevice) and "address" not in calls[-1]


async def test_ble_alternates_fresh_scans_with_os_connected(monkeypatch):
    calls = []

    async def fake_create_ble(**kw):
        calls.append("device" if "device" in kw else "scan")
        return None

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", fake_create_ble)
    link = NodeLink(NodeConfig(transport="ble", ble_direct_after=3))

    async def finder():
        return FakeBleDevice()

    link._find_connected_ble = finder
    for _ in range(9):
        with pytest.raises(LinkError):
            await link._create()
        link._fail_streak += 1
    # a marginal link: the OS keeps listing the peripheral, the bridge still mostly scans
    assert calls == ["scan"] * 3 + ["device", "scan", "scan", "device", "scan", "scan"]


async def test_find_connected_ble_matches_name_and_address(monkeypatch):
    import bleak

    class Dev:
        def __init__(self, name, address): self.name, self.address = name, address

    class Adapter:
        devs = [Dev("Other", "x"), Dev("MeshCore-SparkNode", "AA:BB:CC:DD:EE:FF")]

        @classmethod
        async def get(cls, **kw): return cls()
        async def get_connected_devices(self, uuids): return self.devs

    monkeypatch.setattr(bleak, "BleakAdapter", Adapter, raising=False)
    assert (await NodeLink(NodeConfig(ble_name="SparkNode"))._find_connected_ble()).name == "MeshCore-SparkNode"
    assert await NodeLink(NodeConfig(ble_name="Nope"))._find_connected_ble() is None
    assert (await NodeLink(NodeConfig(ble_address="aa:bb:cc:dd:ee:ff"))._find_connected_ble()).name == "MeshCore-SparkNode"


async def test_exits_after_giving_up(stub):
    port = stub.port
    await stub.stop()
    now = [0.0]
    cfg = NodeConfig(transport="tcp", tcp_port=port, command_timeout=1, backoff_start=0.01,
                     backoff_max=0.02, give_up_minutes=10)

    async def fake_sleep(s):
        now[0] += 200  # each backoff "takes" 200 s
        await asyncio.sleep(0)

    link = NodeLink(cfg, None, clock=lambda: T0, sleep=fake_sleep, monotonic=lambda: now[0])
    with pytest.raises(LinkGaveUp):
        await asyncio.wait_for(link.run(), 10)
    assert now[0] >= 600


def _hanging_ble(monkeypatch):
    async def hang(**kw):
        await asyncio.Event().wait()

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", hang)


async def test_connect_timeout_is_a_failed_attempt(monkeypatch):
    _hanging_ble(monkeypatch)
    link = NodeLink(NodeConfig(transport="ble", ble_name="SparkNode", connect_timeout=0.2))
    with pytest.raises(LinkError, match="timed out"):
        await asyncio.wait_for(link.connect(), 5)
    assert not link.connected and link.mc is None


async def test_run_retries_after_hung_connect(monkeypatch):
    calls = []

    async def hang(**kw):
        calls.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", hang)
    cfg = NodeConfig(transport="ble", ble_name="SparkNode", connect_timeout=0.1, backoff_start=0.01,
                     backoff_max=0.02, give_up_minutes=0)
    link = NodeLink(cfg)
    task = asyncio.ensure_future(link.run())
    await asyncio.sleep(0.6)
    link.stop()
    await asyncio.wait_for(task, 5)
    assert len(calls) >= 2 and link._fail_streak >= 2


async def test_watchdog_gives_up_even_if_connect_never_returns(monkeypatch):
    # connect_timeout is huge, so only the watchdog can end this
    _hanging_ble(monkeypatch)
    now = [0.0]
    cfg = NodeConfig(transport="ble", ble_name="SparkNode", connect_timeout=3600, give_up_minutes=10,
                     watchdog_seconds=0.01)
    link = NodeLink(cfg, None, monotonic=lambda: now[0])
    task = asyncio.ensure_future(link.run())
    await asyncio.sleep(0.1)
    assert not task.done()
    now[0] = 601.0  # ten minutes of wall clock pass with the connect still awaiting
    with pytest.raises(LinkGaveUp):
        await asyncio.wait_for(task, 5)


async def test_hung_command_times_out():
    link = NodeLink(NodeConfig(command_timeout=0.1))

    async def never():
        await asyncio.Event().wait()

    with pytest.raises(LinkError, match="timed out"):
        await asyncio.wait_for(link._cmd(never()), 5)


def test_spark_node_version_and_sender_support():
    from spark_bridge.nodelink import spark_node_version
    assert spark_node_version("v1.18.0-SparkNode1") == (1, 0)
    assert spark_node_version("v1.18.0-SparkNode1.1") == (1, 1)
    assert spark_node_version("v1.18.0-SparkNode1.2") == (1, 2)
    assert spark_node_version("v1.18.0-SparkNode1.10") == (1, 10)
    assert spark_node_version("v1.18.0-SparkNode1.3") == (1, 3)
    assert spark_node_version("v1.18.0-SparkNode1.3 (patch 1)") == (1, 3)
    assert spark_node_version("v1.18.0-SparkNode1.3.1") == (1, 3)
    # the old development label is older than every release, not "2.0"
    assert spark_node_version("v1.18.0-SparkNode2") == (0, 2) < (1, 2)
    assert spark_node_version("v1.18.0-SparkNode2.0") == (2, 0)
    assert spark_node_version("v1.18.0-SparkNodeX") is None
    # the SpArk spelling (1.3 on) and any case parse the same; the dev label stays old either way
    assert spark_node_version("v1.18.0-SpArkNode1.3") == (1, 3)
    assert spark_node_version("v1.18.0-sparknode1.2") == (1, 2)
    assert spark_node_version("v1.18.0-SpArkNode1.3 (patch 1)") == (1, 3)
    assert spark_node_version("v1.18.0-SpArkNode2") == (0, 2)
    assert spark_node_version("v1.18.0") is None and spark_node_version("") is None


async def test_old_firmware_ignores_sender_name(stub):
    stub.version = "v1.18.0-SparkNode1.1"
    link = mklink(stub)
    await link.connect()
    try:
        assert not link.supports_sender
        await link.send_public("Hallo", sender="BadPirate")   # what an old node does with the trailer
        assert stub.transmitted == ["SparkNode: Hallo"]
    finally:
        await link.close()
