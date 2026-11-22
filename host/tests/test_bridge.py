"""Bridge logic with a fake node and a fake ChatPort: composable per-channel behaviour."""
import pytest

from spark_bridge.bridge import (REACT_DOWN, REACT_FAILED, REACT_LIMITED, REACT_TRUNCATED, Bridge,
                                 BridgeError, Inbound)
from spark_bridge.config import Config
from spark_bridge.nodelink import ChannelMsg, LinkError, RoomPost
from spark_bridge.store import Store

OWN = "a1b2c3d4"
GUILD = 1
READ, ADMIN, CHAT, ROOMCH = 100, 200, 300, 400


class FakeNode:
    def __init__(self):
        self.connected = True
        self.node_name = "SparkNode"
        self.version = "v1.18.0-SparkNode1"
        self.own_author = OWN
        self.public: list[str] = []
        self.room: list[str] = []
        self.cli_calls: list[str] = []
        self.fail = False
        self.backlog: list = []
        self.handlers = None

    async def send_public(self, t):
        if self.fail: raise LinkError("boom")
        self.public.append(t)

    async def post_room(self, t):
        if self.fail: raise LinkError("boom")
        self.room.append(t)

    async def cli(self, c):
        self.cli_calls.append(c)
        return "> ok"

    async def drain(self):
        for item in self.backlog:
            if isinstance(item, RoomPost):
                await self.handlers.on_room_post(item)
            else:
                await self.handlers.on_channel_msg(item)
        return len(self.backlog)


class FakeChat:
    def __init__(self):
        self.sent: list[tuple[int, str, str]] = []
        self.reacts: list[tuple[int, int, str]] = []
        self.notices: list[tuple[int, str]] = []

    async def send_as(self, ch, a, t): self.sent.append((ch, a, t))
    async def react(self, ch, m, e): self.reacts.append((ch, m, e))
    async def notice(self, ch, t): self.notices.append((ch, t))


class Clock:
    t = 1_700_000_000.0
    def __call__(self): return self.t


@pytest.fixture
def env():
    node, chat, clock = FakeNode(), FakeChat(), Clock()
    b = Bridge(Store(), node, chat, Config(), clock)
    node.handlers = b
    return b, node, chat, clock


def cfg(b, ch, **kw):
    return b.configure(ch, GUILD, 1, **kw)


def msg(ch, text, user=7, name="robin", mid=1):
    return Inbound(GUILD, ch, mid, user, name, text)


# ---- the owner's two example setups -------------------------------------------------
async def test_broad_read_only_channel(env):
    b, node, chat, _ = env
    cfg(b, READ, receive_source="both", send_target="none")
    await b.on_channel_msg(ChannelMsg(10, "Bob: hello mesh"))
    await b.on_room_post(RoomPost(11, "deadbeef", "room says hi"))
    assert chat.sent == [(READ, "Bob", "hello mesh"), (READ, "node deadbeef", "room says hi")]
    await b.on_discord_message(msg(READ, "can anyone hear me"))
    assert node.public == [] and node.room == [] and chat.reacts == []


async def test_private_admin_channel_plain_public_send(env):
    b, node, chat, _ = env
    cfg(b, ADMIN, admin=True, send_target="public", prefix=False)
    assert b.store.get_channel(ADMIN).report  # admin on enables report by default
    await b.on_discord_message(msg(ADMIN, "CQ from the roof"))
    assert node.public == ["CQ from the roof"] and node.room == []  # no prefix: sent as the node itself
    assert await b.run_cli(ADMIN, "get repeat") == "> ok"


# ---- send --------------------------------------------------------------------------------
@pytest.mark.parametrize("target,pub,room", [("public", 1, 0), ("room", 0, 1), ("both", 1, 1), ("none", 0, 0)])
async def test_send_targets(env, target, pub, room):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target=target)
    await b.on_discord_message(msg(CHAT, "hi"))
    assert len(node.public) == pub and len(node.room) == room
    for t in node.public + node.room:
        assert t == "[robin] hi"


async def test_unconfigured_and_empty_ignored(env):
    b, node, chat, _ = env
    await b.on_discord_message(msg(999, "hi"))
    cfg(b, CHAT, send_target="both")
    await b.on_discord_message(msg(CHAT, "   \n "))
    assert node.public == node.room == []


async def test_truncation_multibyte_and_reaction(env):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target="both", rate_mode="none")
    await b.on_discord_message(msg(CHAT, "é" * 200, mid=5))
    assert len(node.room[0].encode()) <= 151 and node.room[0].startswith("[robin] é")
    assert len(node.public[0].encode()) <= 160 - len("SparkNode: ")
    node.room[0].encode().decode("utf-8")  # valid UTF-8
    assert (CHAT, 5, REACT_TRUNCATED) in chat.reacts


async def test_rate_limit_per_channel_per_user_none(env):
    b, node, chat, clock = env
    cfg(b, CHAT, send_target="room", rate_mode="channel", rate_per_minute=2)
    for i in range(3):
        await b.on_discord_message(msg(CHAT, f"m{i}", user=i, mid=i))
    assert len(node.room) == 2 and chat.reacts == [(CHAT, 2, REACT_LIMITED)]
    clock.t += 61
    await b.on_discord_message(msg(CHAT, "later", mid=9))
    assert len(node.room) == 3

    cfg(b, ROOMCH, send_target="room", rate_mode="user", rate_per_minute=1)
    await b.on_discord_message(msg(ROOMCH, "a", user=1, mid=20))
    await b.on_discord_message(msg(ROOMCH, "b", user=2, mid=21))  # other user: fine
    await b.on_discord_message(msg(ROOMCH, "c", user=1, mid=22))  # same user: limited
    assert len(node.room) == 5 and (ROOMCH, 22, REACT_LIMITED) in chat.reacts

    cfg(b, ADMIN, send_target="room", rate_mode="none")
    for i in range(10):
        await b.on_discord_message(msg(ADMIN, f"x{i}", mid=100 + i))
    assert len(node.room) == 15


async def test_global_public_cap_independent_of_channel_limit(env):
    b, node, chat, clock = env
    cfg(b, CHAT, send_target="public", rate_mode="none")
    for i in range(5):
        await b.on_discord_message(msg(CHAT, f"m{i}", mid=i))
    assert len(node.public) == 3  # burst 3
    assert [r[2] for r in chat.reacts] == [REACT_LIMITED, REACT_LIMITED]
    clock.t += 20  # one token refills (3/min)
    await b.on_discord_message(msg(CHAT, "again", mid=50))
    assert len(node.public) == 4


async def test_link_down_and_failures_react_and_do_not_queue(env):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target="room")
    node.connected = False
    await b.on_discord_message(msg(CHAT, "x", mid=1))
    assert chat.reacts == [(CHAT, 1, REACT_DOWN)]
    node.connected = True
    node.fail = True
    await b.on_discord_message(msg(CHAT, "y", mid=2))
    assert (CHAT, 2, REACT_FAILED) in chat.reacts
    node.fail = False
    await b.on_discord_message(msg(CHAT, "z", mid=3))
    assert node.room == ["[robin] z"]  # nothing was queued from before


# ---- receive -----------------------------------------------------------------------------
async def test_receive_sources_are_separate(env):
    b, node, chat, _ = env
    cfg(b, READ, receive_source="public")
    cfg(b, ROOMCH, receive_source="room")
    cfg(b, CHAT, receive_source="none")
    await b.on_channel_msg(ChannelMsg(1, "Bob: p"))
    await b.on_room_post(RoomPost(2, "deadbeef", "r"))
    assert chat.sent == [(READ, "Bob", "p"), (ROOMCH, "node deadbeef", "r")]


async def test_name_resolution(env):
    b, node, chat, _ = env
    cfg(b, ROOMCH, receive_source="room")
    await b.on_advert("deadbeef" + "00" * 28, "Alice")
    await b.on_room_post(RoomPost(1, "deadbeef", "hi"))
    await b.on_room_post(RoomPost(2, OWN, "from cli"))  # the room itself
    await b.on_room_post(RoomPost(3, "01020304", "stranger"))
    assert [(a, t) for _, a, t in chat.sent] == [("Alice", "hi"), ("SparkNode", "from cli"), ("node 01020304", "stranger")]


async def test_dedup_after_replay(env):
    b, node, chat, _ = env
    cfg(b, ROOMCH, receive_source="room")
    p = RoomPost(5, "deadbeef", "once")
    await b.on_room_post(p)
    await b.on_room_post(RoomPost(5, "deadbeef", "once"))  # replay after reconnect
    await b.on_channel_msg(ChannelMsg(9, "Bob: x"))
    assert len(chat.sent) == 1


async def test_first_run_no_backfill_then_forwards(env):
    b, node, chat, _ = env
    cfg(b, ROOMCH, receive_source="both")
    node.backlog = [RoomPost(1, "deadbeef", "old post"), ChannelMsg(2, "Bob: old chat")]
    await b.on_link_up()
    assert chat.sent == [] and b.store.get_meta("initialized")
    # reconnect: replay of the same posts is deduplicated, a new one is forwarded
    node.backlog = [RoomPost(1, "deadbeef", "old post"), RoomPost(3, "deadbeef", "new post")]
    await b.on_link_up()
    assert [t for _, _, t in chat.sent] == ["new post"]


async def test_own_discord_posts_replayed_after_reconnect_are_not_echoed(env):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target="room")
    cfg(b, READ, receive_source="room")
    await b.on_discord_message(msg(CHAT, "hello"))
    chat.sent.clear()
    # hours later the node replays our post authored by the room with its own room-clock ts
    b.clock.t += 7200
    b._recent.clear()
    await b.on_room_post(RoomPost(1_700_007_000, OWN, "[robin] hello"))
    assert chat.sent == []
    await b.on_room_post(RoomPost(1_700_007_001, OWN, "posted on the CLI"))  # a genuine room.post
    assert [t for _, _, t in chat.sent] == ["posted on the CLI"]


async def test_discord_traffic_mirrored_to_other_receiving_channels(env):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target="public", receive_source="public")
    cfg(b, READ, receive_source="both")
    cfg(b, ROOMCH, receive_source="room")
    await b.on_discord_message(msg(CHAT, "hello"))
    assert chat.sent == [(READ, "robin", "hello")]  # not to itself, not to a room-only channel


async def test_recent_filter_drops_echo_of_public_text(env):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target="public")
    cfg(b, READ, receive_source="public")
    await b.on_discord_message(msg(CHAT, "ping"))
    chat.sent.clear()
    await b.on_channel_msg(ChannelMsg(1, "SparkNode: [robin] ping"))  # should not normally happen
    assert chat.sent == []


# ---- admin / CLI -----------------------------------------------------------------------------
async def test_cli_only_in_admin_channels(env):
    b, node, chat, _ = env
    with pytest.raises(BridgeError):
        await b.run_cli(CHAT, "get repeat")
    cfg(b, CHAT, admin=False, send_target="room")
    with pytest.raises(BridgeError):
        await b.run_cli(CHAT, "get repeat")
    cfg(b, CHAT, admin=True)
    assert await b.run_cli(CHAT, " get repeat ") == "> ok"
    cfg(b, CHAT, admin=False)
    with pytest.raises(BridgeError):
        await b.run_cli(CHAT, "get repeat")


@pytest.mark.parametrize("cmd", ["password x", "set guest.password abc", "set prv.key 00", "erase",
                                 "start ota", "set ble.pin 123456", "SET  BLE.PIN 1", "get prv.key"])
async def test_cli_denylist(env, cmd):
    b, node, _, _ = env
    cfg(b, ADMIN, admin=True)
    with pytest.raises(BridgeError):
        await b.run_cli(ADMIN, cmd)
    assert node.cli_calls == []
    b.cfg.allow_dangerous_cli = True
    await b.run_cli(ADMIN, cmd)
    assert node.cli_calls == [cmd]


async def test_cli_not_blocked_lookalikes(env):
    b, node, _, _ = env
    cfg(b, ADMIN, admin=True)
    await b.run_cli(ADMIN, "get password.hint")  # "password" only as a whole command word
    await b.run_cli(ADMIN, "erased")
    assert len(node.cli_calls) == 2


async def test_cli_link_down(env):
    b, node, _, _ = env
    cfg(b, ADMIN, admin=True)
    node.connected = False
    with pytest.raises(BridgeError):
        await b.run_cli(ADMIN, "ver")


# ---- config command semantics ---------------------------------------------------------------------
def test_configure_validation_and_status(env):
    b, *_ = env
    with pytest.raises(BridgeError):
        cfg(b, CHAT, send_target="everyone")
    with pytest.raises(BridgeError):
        cfg(b, CHAT, receive_source="x")
    with pytest.raises(BridgeError):
        cfg(b, CHAT, rate_mode="sometimes")
    with pytest.raises(BridgeError):
        cfg(b, CHAT, rate_mode="channel", rate_per_minute=0)
    c = cfg(b, CHAT, send_target="both", prefix=False, rate_mode="user", rate_per_minute=5, receive_source="room")
    s = b.status(CHAT)
    assert "both, prefix off, rate limit 5/min per user" in s and "receive (mesh to Discord): room" in s
    c = cfg(b, CHAT, rate_mode="channel")  # per_minute omitted keeps the stored value
    assert c.rate_per_minute == 5
    assert "send (Discord to mesh): none" in b.status(555)


async def test_channels_are_independent(env):
    b, node, chat, _ = env
    cfg(b, CHAT, send_target="public")
    cfg(b, ROOMCH, send_target="room", prefix=False)
    await b.on_discord_message(msg(CHAT, "a"))
    await b.on_discord_message(msg(ROOMCH, "b"))
    assert node.public == ["[robin] a"] and node.room == ["b"]


# ---- link notices -------------------------------------------------------------------------------------
async def test_link_notices_once_per_ten_minutes(env):
    b, node, chat, clock = env
    cfg(b, ADMIN, admin=True)
    cfg(b, READ, receive_source="both")  # report off: no notices
    await b.on_link_down()
    await b.on_link_up()
    assert [n[0] for n in chat.notices] == [ADMIN, ADMIN] and "down" in chat.notices[0][1]
    clock.t += 60
    await b.on_link_down()  # within 10 min: suppressed, and so is the matching "up"
    await b.on_link_up()
    assert len(chat.notices) == 2
    clock.t += 700
    await b.on_link_down()
    assert len(chat.notices) == 3


async def test_up_notice_survives_process_restart(env):
    """Down notice posted by one process; a new process (same DB) posts "up again" on first link-up."""
    b, node, chat, clock = env
    cfg(b, ADMIN, admin=True)
    await b.on_link_down()
    assert len(chat.notices) == 1 and "down" in chat.notices[0][1]
    b2 = Bridge(b.store, node, chat, Config(), clock)  # restarted bridge, same database
    node.handlers = b2
    await b2.on_link_up()
    assert len(chat.notices) == 2 and "up again" in chat.notices[1][1]
    await b2.on_link_up()  # only once
    assert len(chat.notices) == 2


async def test_pair_rate_limit_persists_and_suppressed_pairs_stay_silent(env):
    b, node, chat, clock = env
    cfg(b, ADMIN, admin=True)
    await b.on_link_down()
    await b.on_link_up()
    clock.t += 60
    b2 = Bridge(b.store, node, chat, Config(), clock)
    await b2.on_link_down()  # within 10 min of the previous pair, even across a restart
    await b2.on_link_up()
    assert len(chat.notices) == 2
    clock.t += 700
    await b2.on_link_down()
    await b2.on_link_up()
    assert len(chat.notices) == 4


# ---- /spark where (SparkNode1 member locations) -------------------------------------------
from spark_bridge.bridge import parse_where, format_where  # noqa: E402


def test_parse_where_lines_pages_and_names_with_spaces():
    fixes, more = parse_where("Nova 5m 12.345670,3.456780 s8\nAnna Sophie 2h -0.500000,-3.456780 s0\nmore: where 2")
    assert more == 2
    assert [(f.name, f.age, f.lat, f.lon, f.sats) for f in fixes] == [
        ("Nova", "5m", 12.34567, 3.45678, 8), ("Anna Sophie", "2h", -0.5, -3.45678, 0)]
    assert fixes[0].maps_url == "https://maps.google.com/?q=12.345670,3.456780"
    fixes, more = parse_where("Papa never")
    assert more is None and fixes[0].age == "never" and fixes[0].maps_url is None
    assert parse_where("no locations") == ([], None)
    with pytest.raises(BridgeError):
        parse_where("Err - no member 'x'")


def test_format_where():
    fixes, _ = parse_where("Nova 30s 12.345670,3.456780 s8\nPapa never\nTom 12m 52.000000,4.000000 s0")
    text = format_where(fixes)
    assert "**Nova**: just now, 8 satellites\n<https://maps.google.com/?q=12.345670,3.456780>" in text
    assert "**Papa**: no location yet" in text
    assert "**Tom**: 12m ago\n<https://maps.google.com/?q=52.000000,4.000000>" in text
    assert format_where([]) == "No locations known yet."


async def test_where_admin_only_pages_and_name(env):
    b, node, _, _ = env
    pages = {"where": "Nova 5m 12.345670,3.456780 s8\nmore: where 1", "where 1": "Tom 1h 52.1,4.2 s5",
             "where nova": "Nova 5m 12.345670,3.456780 s8"}

    async def cli(c):
        node.cli_calls.append(c)
        return pages[c]
    node.cli = cli
    cfg(b, CHAT, receive_source="room")
    with pytest.raises(BridgeError):
        await b.where(CHAT)            # kids' locations never in a non-admin channel
    assert node.cli_calls == []
    cfg(b, ADMIN, admin=True)
    text = await b.where(ADMIN)
    assert node.cli_calls == ["where", "where 1"]
    assert "**Nova**" in text and "**Tom**: 1h ago" in text and "q=52.100000,4.200000" in text
    node.cli_calls.clear()
    text = await b.where(ADMIN, "  nova \n")
    assert node.cli_calls == ["where nova"] and "**Nova**" in text and "Tom" not in text
    node.connected = False
    with pytest.raises(BridgeError):
        await b.where(ADMIN)


# ---- names from the room's member directory (SparkNode1 `members`) ------------------------
from spark_bridge.bridge import parse_members  # noqa: E402


def test_parse_members():
    found, more = parse_members("BEA76F5A Charlie 2m\n01020304 Papa Bear* 3h\nAABBCCDD - 1d\nmore: members 3")
    assert found == [("bea76f5a", "Charlie", False), ("01020304", "Papa Bear", True)] and more == 3
    assert parse_members("> no members") == ([], None)
    assert parse_members("Err - unknown command") == ([], None)


async def test_room_author_named_from_member_directory(env):
    b, node, chat, clock = env
    pages = {"members": "BEA76F5A Charlie 2m\nmore: members 1", "members 1": "01020304 Nova 5m"}

    async def cli(c):
        node.cli_calls.append(c)
        return pages.get(c, "> ok")
    node.cli = cli
    cfg(b, ROOMCH, receive_source="room")
    await b.on_link_up()   # learns the directory names first
    assert node.cli_calls == ["members", "members 1"]
    await b.on_room_post(RoomPost(1, "bea76f5a", "hi from the t-deck"))
    assert chat.sent[-1][1:] == ("Charlie", "hi from the t-deck")
    # a member who joined after link-up: one refresh, rate limited
    node.cli_calls.clear()
    pages["members 1"] = "01020304 Nova 5m\n0a0b0c0d Tom 1s"
    clock.t += 400
    await b.on_room_post(RoomPost(2, "0a0b0c0d", "new kid"))
    assert chat.sent[-1][1] == "Tom" and node.cli_calls == ["members", "members 1"]
    node.cli_calls.clear()
    await b.on_room_post(RoomPost(3, "deadbeef", "stranger"))   # within 5 min: no new refresh
    assert chat.sent[-1][1] == "node deadbeef" and node.cli_calls == []
    # an advert still wins for names the directory does not have
    await b.on_advert("deadbeef" + "00" * 28, "Alice")
    await b.on_room_post(RoomPost(4, "deadbeef", "named"))
    assert chat.sent[-1][1] == "Alice"


async def test_deleted_channel_rows_are_removed_once(env, caplog):
    from spark_bridge.bridge import ChannelGone
    b, node, chat, clock = env
    cfg(b, 1111111111111111111, receive_source="room", report=True)
    cfg(b, 2222222222222222222, receive_source="room")
    cfg(b, 70, receive_source="room", report=True)
    gone = {1111111111111111111, 2222222222222222222}

    async def send_as(ch, a, t):
        if ch in gone:
            raise ChannelGone(ch)
        chat.sent.append((ch, a, t))

    async def notice(ch, t):
        if ch in gone:
            raise ChannelGone(ch)
        chat.notices.append((ch, t))

    chat.send_as, chat.notice = send_as, notice
    with caplog.at_level("WARNING", logger="spark_bridge"):
        await b.on_room_post(RoomPost(ts=1, author="aabbccdd", text="hello"))
        await b.on_room_post(RoomPost(ts=2, author="aabbccdd", text="again"))
        await b._notify("note")
    assert [c.channel_id for c in b.store.all_channels()] == [70]
    assert [s[2] for s in chat.sent] == ["hello", "again"]
    assert chat.notices == [(70, "note")]
    removed = [r for r in caplog.records if "removed from the bridge" in r.getMessage()]
    assert len(removed) == 2
    assert b.forget_channel(1111111111111111111) is False   # already gone: no second log line
