"""discordbot imports and constructs without a token or a network connection."""
import asyncio

import pytest

from spark_bridge.bridge import Bridge, Inbound
from spark_bridge.config import Config
from spark_bridge.discordbot import SparkClient, webhook_username
from spark_bridge.store import Store


def test_webhook_username_rules():
    assert webhook_username("Bob") == "Bob"
    assert "discord" not in webhook_username("Discord Dave").lower()
    assert "clyde" not in webhook_username("clyde").lower()
    assert len(webhook_username("x" * 200)) == 80
    assert len(webhook_username("")) >= 2


async def test_client_constructs_with_group_and_commands():
    cfg = Config()
    cfg.discord.allowed_guild_ids = [1]
    client = SparkClient(cfg)
    try:
        grp = client.tree.get_command("spark")
        names = {c.name for c in grp.commands}
        assert names == {"status", "admin", "send", "prefix", "ratelimit", "receive", "report", "footer", "health", "cli",
                         "where", "alias"}
        assert {c.name for c in grp.get_command("alias").commands} == {"set", "list", "del"}
        assert client.intents.message_content
        assert client.guild_ok(1) and not client.guild_ok(2) and not client.guild_ok(None)
    finally:
        await client.close()


async def test_message_filter_ignores_bots_and_other_guilds():
    class Msg:
        pass

    class Fake:
        def __init__(self): self.calls = []
        async def on_discord_message(self, m): self.calls.append(m)

    cfg = Config(); cfg.discord.allowed_guild_ids = [1]
    client = SparkClient(cfg)
    client.bridge = Fake()
    import discord

    def mk(bot=False, webhook=None, guild=1, text="hi"):
        m = Msg()
        m.author = type("A", (), {"bot": bot, "id": 5, "display_name": "kev"})()
        m.webhook_id = webhook
        m.guild = type("G", (), {"id": guild})()
        m.channel = type("C", (), {"id": 9})()
        m.id = 3
        m.type = discord.MessageType.default
        m.clean_content = text
        return m

    await client.on_message(mk())
    await client.on_message(mk(bot=True))
    await client.on_message(mk(webhook=77))
    await client.on_message(mk(guild=2))
    await client.on_message(mk(text="  "))
    assert [c.text for c in client.bridge.calls] == ["hi"] and client.bridge.calls[0].author == "kev"
    await client.close()


class FakeIt:
    def __init__(self, manage=True, guild=1, channel=50):
        self.guild_id, self.channel_id = guild, channel
        self.user = type("U", (), {"id": 9})()
        self.permissions = type("P", (), {"manage_channels": manage})()
        self.out: list[tuple[str, bool]] = []
        self.views: list = []
        self.edits: list = []
        it = self

        class R:
            async def send_message(self, text, ephemeral=False, allowed_mentions=None, view=None):
                it.out.append((text, ephemeral))
                it.views.append(view)
            async def defer(self, thinking=False): it.deferred = True
            async def edit_message(self, content=None, view=None): it.edits.append(content)
        class F:
            async def send(self, text, ephemeral=False, allowed_mentions=None, view=None):
                it.out.append((text, ephemeral))
                it.views.append(view)
        self.response, self.followup = R(), F()


async def test_slash_commands_permissions_and_effects():
    from discord import app_commands as ac
    cfg = Config(); cfg.discord.allowed_guild_ids = [1]
    client = SparkClient(cfg)
    node = type("N", (), {"connected": True, "node_name": "SparkNode", "version": "v", "own_author": "aa"})()

    async def cli(c):
        return "> on"
    node.cli = cli
    br = Bridge(Store(), node, client.chat, cfg)
    client.bridge = br
    grp = client.tree.get_command("spark")
    cmd = lambda n: grp.get_command(n).callback  # noqa: E731
    ch = ac.Choice

    it = FakeIt(manage=False)
    await cmd("send")(it, ch(name="public", value="public"))
    assert "Manage Channels" in it.out[0][0] and it.out[0][1] and br.store.get_channel(50) is None

    it = FakeIt(guild=2)
    await cmd("status")(it)
    assert "not allowed" in it.out[0][0]

    it = FakeIt()
    await cmd("send")(it, ch(name="both", value="both"))   # needs confirmation: warning + buttons
    assert it.out[0][1] and "credentials and identity" in it.out[0][0] and it.views[0] is not None
    assert br.store.get_channel(50) is None
    await cmd("send")(it, ch(name="both", value="both"), True)
    await cmd("prefix")(it, ch(name="off", value=0))
    await cmd("ratelimit")(it, ch(name="user", value="user"), 4)
    await cmd("receive")(it, ch(name="public", value="public"))
    c = br.store.get_channel(50)
    assert (c.send_target, c.prefix, c.rate_mode, c.rate_per_minute, c.receive_source) == ("both", False, "user", 4, "public")
    assert "both, prefix off, rate limit 4/min per user" in it.out[-1][0]

    it = FakeIt(manage=False)  # anyone can read status and, once admin is on, use cli
    await cmd("cli")(it, "get repeat")
    assert it.out[0][1] and "not an admin" in it.out[0][0]
    await cmd("admin")(FakeIt(), ch(name="on", value=1))
    it = FakeIt(manage=False)
    await cmd("cli")(it, "get repeat")
    assert "> on" in it.out[0][0] and not it.out[0][1] and it.deferred

    async def where_cli(c):
        return "Nova 5m 12.345670,3.456780 s8"
    node.cli = where_cli
    it = FakeIt(manage=False)
    await cmd("where")(it, None)
    assert "**Nova**" in it.out[0][0] and "maps.google.com/?q=12.345670,3.456780" in it.out[0][0] and it.deferred
    await client.close()


def _not_found(code):
    import discord
    resp = type("R", (), {"status": 404, "reason": "Not Found"})()
    return discord.NotFound(resp, {"code": code, "message": "Unknown Channel"})


async def test_startup_prune_and_channel_delete_event():
    from spark_bridge.bridge import ChannelGone
    from spark_bridge.discordbot import DiscordChat
    cfg = Config(); cfg.discord.allowed_guild_ids = [1]
    client = SparkClient(cfg)
    store = Store()
    client.bridge = Bridge(store, node=None, chat=None, cfg=cfg)
    for cid in (1111111111111111111, 2222222222222222222, 60, 61, 62):
        client.bridge.configure(cid, 1, 1, receive_source="room")
    deleted = {1111111111111111111, 2222222222222222222}

    async def fetch_channel(cid):
        if cid in deleted:
            raise _not_found(10003)
        if cid == 61:
            raise _not_found(10008)       # some other 404: keep the row
        return object()
    client.fetch_channel = fetch_channel
    try:
        assert await client.prune_channels() == 2
        assert [c.channel_id for c in store.all_channels()] == [60, 61, 62]
        assert await client.prune_channels() == 0   # nothing left to remove, no retry loop
        await client.on_guild_channel_delete(type("Ch", (), {"id": 62})())
        assert [c.channel_id for c in store.all_channels()] == [60, 61]

        chat = DiscordChat(client, "spark")
        with pytest.raises(ChannelGone):
            await chat.notice(1111111111111111111, "x")
    finally:
        await client.close()


async def test_confirm_button_and_alias_commands():
    from discord import app_commands as ac
    from spark_bridge.discordbot import ConfirmPublicView
    cfg = Config(); cfg.discord.allowed_guild_ids = [1]
    client = SparkClient(cfg)
    node = type("N", (), {"connected": True, "node_name": "SparkNode", "version": "v", "own_author": "aa"})()
    br = Bridge(Store(), node, client.chat, cfg)
    client.bridge = br
    grp = client.tree.get_command("spark")

    it = FakeIt()
    await grp.get_command("send").callback(it, ac.Choice(name="public", value="public"))
    view = it.views[0]
    assert isinstance(view, ConfirmPublicView) and view.target == "public"

    other = FakeIt(); other.user = type("U", (), {"id": 10})()
    assert not await view.interaction_check(other) and "Only the admin" in other.out[0][0]
    no_perm = FakeIt(manage=False)
    assert not await view.interaction_check(no_perm)
    assert await view.interaction_check(FakeIt())

    press = FakeIt()
    await view.cancel.callback(press)
    assert press.edits == ["Cancelled: send is unchanged."] and br.store.get_channel(50) is None
    press = FakeIt()
    await view.confirm.callback(press)
    assert press.edits == ["Confirmed: send public."]
    assert br.store.get_channel(50).send_target == "public"
    assert br.store.last_audit(50, "send public on")[1] == 9
    assert "Public enabled by <@9>" in press.out[0][0]

    alias = grp.get_command("alias")
    user = type("User", (), {"id": 77})()
    it = FakeIt(manage=False)
    await alias.get_command("set").callback(it, user, "Kev")
    assert "Manage Channels" in it.out[0][0] and br.store.get_alias(77) is None
    it = FakeIt()
    await alias.get_command("set").callback(it, user, "Kev")
    assert br.store.get_alias(77) == "Kev"
    it = FakeIt(manage=False)
    await alias.get_command("list").callback(it)
    assert "<@77>: Kev" in it.out[0][0]
    it = FakeIt()
    await alias.get_command("del").callback(it, user)
    assert br.store.get_alias(77) is None
    await client.close()


async def test_closed_client_raises_chat_closed():
    from spark_bridge.bridge import ChatClosed

    cfg = Config()
    client = SparkClient(cfg)
    await client.close()   # shutdown: the session is gone
    with pytest.raises(ChatClosed):
        await client.chat.notice(123, "Link to the node is **down**; reconnecting.")
    with pytest.raises(ChatClosed):
        await client.chat.send_as(123, "Bob", "hi")

    class Ch:
        async def send(self, *a, **k):
            raise RuntimeError("Session is closed")

    client2 = SparkClient(cfg)
    try:
        client2.get_channel = lambda cid: Ch()
        with pytest.raises(ChatClosed):
            await client2.chat.notice(1, "x")
    finally:
        await client2.close()
