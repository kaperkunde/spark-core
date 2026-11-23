"""Bridge + real meshcore NodeLink + stub node, fake Discord."""
import asyncio

from spark_bridge.bridge import Bridge, Inbound
from spark_bridge.config import Config
from spark_bridge.nodelink import NodeLink
from spark_bridge.store import Store

from test_bridge import FakeChat
from test_nodelink import wait_for

T0 = 1700000000


async def test_end_to_end(stub):
    cfg = Config()
    cfg.node.transport, cfg.node.tcp_port = "tcp", stub.port
    cfg.node.backoff_start, cfg.node.backoff_max, cfg.node.command_timeout = 0.05, 0.2, 3
    chat = FakeChat()
    node = NodeLink(cfg.node, clock=lambda: T0)
    bridge = Bridge(Store(), node, chat, cfg)
    node.handlers = bridge
    bridge.configure(1, 1, confirm=True, receive_source="both", send_target="both", prefix=True)
    stub.inject_room_post("deadbeef", "backlog post", ts=T0 - 10)  # first run: not forwarded
    task = asyncio.create_task(node.run())
    try:
        await wait_for(lambda: node.connected and bridge.store.get_meta("initialized"))
        assert chat.sent == []
        # SpArkNode1.3: the route comes along; a known repeater is shown by name
        bridge.store.set_name("1112aaaa", "NL-AMS-DAR1", 2)
        stub.inject_channel("Bob", "hello", ts=T0, hops=2, hash_size=2, path=bytes.fromhex("1112c3d4"), snr=5.0)
        await wait_for(lambda: chat.sent)
        assert chat.sent[0] == (1, "Bob", "hello\n-# 2 hops · SNR 5.0 dB · via NL-AMS-DAR1 → c3d4")
        assert node.features == 2   # route on, no raw RX feed without an observer
        row = bridge.store.seen_meta("c", T0, __import__("spark_bridge.textlimits").textlimits.text_hash("Bob: hello"))
        assert (row["snr"], row["rssi"], row["hops"], row["path"]) == (5.0, -95, 2, "1112,c3d4")
        await bridge.on_discord_message(Inbound(1, 1, 7, 3, "robin", "from discord"))
        # SparkNode1.2: the sender name goes in the frame, the room post keeps the [name] tag
        assert stub.transmitted == ["robin: from discord"] and stub.room_pushed == ["[robin] from discord"]
        bridge.alias_set(3, "Robin Roof")
        await bridge.on_discord_message(Inbound(1, 1, 8, 3, "robin", "again"))
        assert stub.transmitted[-1] == "Robin Roof: again" and stub.room_pushed[-1] == "[Robin Roof] again"
        chat.sent.clear()
        # node drops: bridge reconnects, the replay includes our own post and the backlog; nothing is forwarded
        await stub.drop_connections()
        await wait_for(lambda: node.sessions == 2 and node.connected)
        await asyncio.sleep(0.3)
        assert chat.sent == []
        stub.inject_room_post("deadbeef", "after replay", ts=T0 + 5)
        await wait_for(lambda: chat.sent)
        assert chat.sent[0][2] == "after replay"
    finally:
        node.stop()
        await asyncio.wait_for(task, 5)
