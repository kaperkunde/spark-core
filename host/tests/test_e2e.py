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
    bridge.configure(1, 1, receive_source="both", send_target="both", prefix=True)
    stub.inject_room_post("deadbeef", "backlog post", ts=T0 - 10)  # first run: not forwarded
    task = asyncio.create_task(node.run())
    try:
        await wait_for(lambda: node.connected and bridge.store.get_meta("initialized"))
        assert chat.sent == []
        stub.inject_channel("Bob", "hello", ts=T0)
        await wait_for(lambda: chat.sent)
        assert chat.sent[0] == (1, "Bob", "hello")
        await bridge.on_discord_message(Inbound(1, 1, 7, 3, "robin", "from discord"))
        assert stub.transmitted == ["SparkNode: [robin] from discord"] and stub.room_pushed == ["[robin] from discord"]
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
