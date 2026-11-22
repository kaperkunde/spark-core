import pytest_asyncio

from spark_bridge.stubnode import StubNode

T0 = 1700000000


@pytest_asyncio.fixture
async def stub():
    node = StubNode(clock=lambda: T0)
    await node.start()
    yield node
    await node.stop()
