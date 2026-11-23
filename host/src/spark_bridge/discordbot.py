"""discord.py layer: the only module that imports discord.py.

Slash command group /spark. Configuration commands (and `/spark alias set|del`) need Discord's
Manage Channels permission in the channel (checked here from the interaction's resolved
permissions). Enabling `send public` or `send both` also needs a confirmation (button, or
`confirm:True`) after a warning. `/spark status`,
`/spark health`, `/spark cli` and `/spark where` follow the channel: anyone who can see an admin channel can use
them. Channel visibility in Discord is the only access control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import discord
from discord import app_commands

from .bridge import Bridge, BridgeError, ChannelGone, ChatClosed, Inbound, NeedsConfirmation
from .config import Config

log = logging.getLogger("spark_bridge.discord")

NO_MENTIONS = discord.AllowedMentions.none()
UNKNOWN_CHANNEL = 10003
BANNED_NAME_PARTS = ("discord", "clyde")  # Discord rejects webhook usernames containing these


def webhook_username(author: str) -> str:
    name = " ".join(author.split()) or "mesh"
    for bad in BANNED_NAME_PARTS:
        idx = name.lower().find(bad)
        while idx >= 0:
            name = name[: idx + 1] + "​" + name[idx + 1:]
            idx = name.lower().find(bad, idx + 2)
    if len(name) < 2:
        name += "​"
    return name[:80]


def closed_session(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "session is closed" in str(exc).lower()


class DiscordChat:
    """ChatPort implementation: webhook with bot-message fallback."""

    def __init__(self, client: discord.Client, webhook_name: str):
        self.client = client
        self.webhook_name = webhook_name
        self._hooks: dict[int, discord.Webhook | None] = {}
        self._retry_at: dict[int, float] = {}

    def _check_open(self) -> None:
        if self.client.is_closed():
            raise ChatClosed()

    async def _channel(self, channel_id: int):
        self._check_open()
        ch = self.client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(channel_id)
            except discord.NotFound as exc:
                if exc.code == UNKNOWN_CHANNEL:
                    raise ChannelGone(channel_id) from exc
                raise
        return ch

    async def _guard(self, channel_id: int, coro):
        """A send that fails with 404 Unknown Channel means the channel was deleted."""
        try:
            return await coro
        except discord.NotFound as exc:
            if exc.code == UNKNOWN_CHANNEL:
                self._hooks.pop(channel_id, None)
                raise ChannelGone(channel_id) from exc
            raise
        except RuntimeError as exc:
            if closed_session(exc):   # shutdown or restart: aiohttp session already closed
                raise ChatClosed() from None
            raise

    async def _webhook(self, ch) -> discord.Webhook | None:
        if not isinstance(ch, discord.TextChannel):
            return None
        if ch.id in self._hooks:
            if self._hooks[ch.id] is not None or time.time() < self._retry_at.get(ch.id, 0):
                return self._hooks[ch.id]
        try:
            for wh in await ch.webhooks():
                if wh.name == self.webhook_name and wh.token:  # token only on hooks this bot owns
                    self._hooks[ch.id] = wh
                    return wh
            wh = await ch.create_webhook(name=self.webhook_name, reason="spark-bridge mesh authors")
            self._hooks[ch.id] = wh
            return wh
        except discord.Forbidden:
            log.info("no Manage Webhooks in #%s: falling back to bot messages", ch.name)
        except discord.HTTPException as exc:
            log.warning("webhook lookup failed: %s", exc)
        self._hooks[ch.id] = None
        self._retry_at[ch.id] = time.time() + 300
        return None

    async def send_as(self, channel_id: int, author: str, text: str) -> None:
        ch = await self._channel(channel_id)
        try:
            wh = await self._webhook(ch)
        except RuntimeError as exc:
            if closed_session(exc):
                raise ChatClosed() from None
            raise
        if wh is not None:
            try:
                await wh.send(content=text[:2000], username=webhook_username(author),
                              allowed_mentions=NO_MENTIONS)
                return
            except RuntimeError as exc:
                if closed_session(exc):
                    raise ChatClosed() from None
                raise
            except discord.NotFound:
                self._hooks.pop(channel_id, None)  # deleted: look it up again next time
            except discord.HTTPException as exc:
                log.warning("webhook send failed (%s), using bot message", exc)
        await self._guard(channel_id, ch.send(f"**{discord.utils.escape_markdown(author)}**: {text}"[:2000],
                                              allowed_mentions=NO_MENTIONS))

    async def react(self, channel_id: int, message_id: int, emoji: str) -> None:
        ch = await self._channel(channel_id)
        await self._guard(channel_id, ch.get_partial_message(message_id).add_reaction(emoji))

    async def notice(self, channel_id: int, text: str) -> None:
        ch = await self._channel(channel_id)
        await self._guard(channel_id, ch.send(text[:2000], allowed_mentions=NO_MENTIONS))


class ConfirmPublicView(discord.ui.View):
    """Confirm / Cancel under the Public-send warning. Only the admin who ran the command can
    press it, and they still need Manage Channels when they do."""

    def __init__(self, client: "SparkClient", owner_id: int, target: str, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.client, self.owner_id, self.target = client, owner_id, target

    async def interaction_check(self, it: discord.Interaction) -> bool:
        if it.user.id != self.owner_id:
            await it.response.send_message("Only the admin who ran the command can confirm it.", ephemeral=True)
            return False
        if not it.permissions.manage_channels:
            await it.response.send_message("You need the Manage Channels permission here.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, it: discord.Interaction, button: discord.ui.Button):
        b = self.client.bridge
        assert b
        try:
            b.configure(it.channel_id, it.guild_id, it.user.id, confirm=True, send_target=self.target)
            text = b.status(it.channel_id)
        except BridgeError as exc:
            text = str(exc)
        self.stop()
        await it.response.edit_message(content=f"Confirmed: send {self.target}.", view=None)
        await it.followup.send(text[:1900], allowed_mentions=NO_MENTIONS)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, it: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await it.response.edit_message(content="Cancelled: send is unchanged.", view=None)


def onoff(v: bool) -> str:
    return "on" if v else "off"


class SparkClient(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Developer Portal
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.cfg = cfg
        self.bridge: Bridge | None = None
        self.tree = app_commands.CommandTree(self)
        self.chat = DiscordChat(self, cfg.discord.webhook_name)
        self.tree.add_command(build_group(self))

    def guild_ok(self, guild_id: int | None) -> bool:
        return guild_id is not None and guild_id in self.cfg.discord.allowed_guild_ids

    async def setup_hook(self) -> None:
        if not self.cfg.discord.allowed_guild_ids:
            log.warning("allowed_guild_ids is empty: the bridge will not act in any server")
        for gid in self.cfg.discord.allowed_guild_ids:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                log.error("command sync failed for guild %s: %s", gid, exc)

    async def on_ready(self) -> None:
        log.info("discord ready as %s in %d guild(s)", self.user, len(self.guilds))
        await self.prune_channels()

    async def prune_channels(self) -> int:
        """Drop rows for channels deleted while the bridge was not running (404 Unknown Channel)."""
        if self.bridge is None:
            return 0
        n = 0
        for ch in self.bridge.store.all_channels():
            if self.get_channel(ch.channel_id) is not None:
                continue
            try:
                await self.fetch_channel(ch.channel_id)
            except discord.NotFound as exc:
                if exc.code == UNKNOWN_CHANNEL and self.bridge.forget_channel(ch.channel_id, "Unknown Channel at startup"):
                    n += 1
            except discord.HTTPException as exc:   # Forbidden, outage: keep the row
                log.info("channel %s not checked: %s", ch.channel_id, exc)
        return n

    async def on_guild_channel_delete(self, channel) -> None:
        if self.bridge is not None:
            self.bridge.forget_channel(channel.id, "channel_delete event")

    async def on_message(self, message: discord.Message) -> None:
        if (self.bridge is None or message.author.bot or message.webhook_id is not None
                or message.guild is None or not self.guild_ok(message.guild.id)
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)):
            return
        if not message.clean_content.strip():
            return  # attachments and embeds are not forwarded
        try:
            await self.bridge.on_discord_message(Inbound(
                guild_id=message.guild.id, channel_id=message.channel.id, message_id=message.id,
                user_id=message.author.id, author=message.author.display_name,
                text=message.clean_content))
        except Exception:  # noqa: BLE001
            log.exception("message handling failed")

    async def report_to_channels(self, text: str) -> None:
        assert self.bridge
        for ch in self.bridge.store.all_channels():
            if ch.report and self.guild_ok(ch.guild_id):
                try:
                    await self.chat.notice(ch.channel_id, text)
                except ChatClosed:
                    log.info("Discord is closed; report not posted")
                    return
                except ChannelGone:
                    self.bridge.forget_channel(ch.channel_id, "Unknown Channel (404)")
                except Exception:  # noqa: BLE001
                    log.exception("report to %s failed", ch.channel_id)


def build_group(client: SparkClient) -> app_commands.Group:
    group = app_commands.Group(name="spark", description="Spark Bridge: mesh and Discord", guild_only=True)
    ONOFF = [app_commands.Choice(name="on", value=1), app_commands.Choice(name="off", value=0)]
    TARGETS = [app_commands.Choice(name=n, value=n) for n in ("none", "public", "room", "both")]
    RATES = [app_commands.Choice(name=n, value=n) for n in ("none", "channel", "user")]

    def bridge() -> Bridge:
        assert client.bridge
        return client.bridge

    async def guarded(it: discord.Interaction, manage: bool, fn: Callable[[], Awaitable[str]],
                      slow: bool = False) -> None:
        """Run a command. `slow` defers first (node round trips can exceed Discord's 3 s)."""
        if not client.guild_ok(it.guild_id):
            await it.response.send_message("This server is not allowed to use the bridge.", ephemeral=True)
            return
        if manage and not it.permissions.manage_channels:
            await it.response.send_message("You need the Manage Channels permission here.", ephemeral=True)
            return
        if slow:
            await it.response.defer(thinking=True)
        extra = {}
        try:
            text = await fn()
        except NeedsConfirmation as exc:
            text, ephemeral = str(exc), True
            extra["view"] = ConfirmPublicView(client, it.user.id, exc.target)
        except BridgeError as exc:
            text, ephemeral = str(exc), True
        else:
            ephemeral = False
        text = text[:1900]
        if slow:
            await it.followup.send(text, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS, **extra)
        else:
            await it.response.send_message(text, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS, **extra)

    def setter(it: discord.Interaction, confirm: bool = False, **changes) -> str:
        b = bridge()
        b.configure(it.channel_id, it.guild_id, it.user.id, confirm=confirm, **changes)
        return b.status(it.channel_id)

    @group.command(name="status", description="Show this channel's bridge behaviour")
    async def status(it: discord.Interaction):
        async def f(): return bridge().status(it.channel_id)
        await guarded(it, False, f)

    @group.command(name="admin", description="Allow /spark cli and /spark health in this channel")
    @app_commands.choices(state=ONOFF)
    async def admin(it: discord.Interaction, state: app_commands.Choice[int]):
        async def f(): return setter(it, admin=bool(state.value))
        await guarded(it, True, f)

    @group.command(name="send", description="Discord to mesh: which mesh target this channel writes to")
    @app_commands.choices(target=TARGETS)
    @app_commands.describe(confirm="public/both: confirm the warning (anyone here can then send as your station)")
    async def send(it: discord.Interaction, target: app_commands.Choice[str], confirm: bool = False):
        async def f(): return setter(it, confirm=confirm, send_target=target.value)
        await guarded(it, True, f)

    @group.command(name="prefix", description="Name the sender: Public sender name / room [alias] tag (off = as the node)")
    @app_commands.choices(state=ONOFF)
    async def prefix(it: discord.Interaction, state: app_commands.Choice[int]):
        async def f(): return setter(it, prefix=bool(state.value))
        await guarded(it, True, f)

    @group.command(name="ratelimit", description="Limit messages sent to the mesh from this channel")
    @app_commands.choices(mode=RATES)
    @app_commands.describe(mode="none, per channel, or per user", per_minute="messages per minute (1-600)")
    async def ratelimit(it: discord.Interaction, mode: app_commands.Choice[str], per_minute: int | None = None):
        async def f():
            ch = {"rate_mode": mode.value}
            if per_minute is not None:
                ch["rate_per_minute"] = per_minute
            return setter(it, **ch)
        await guarded(it, True, f)

    @group.command(name="receive", description="Mesh to Discord: which mesh source is copied into this channel")
    @app_commands.choices(source=TARGETS)
    async def receive(it: discord.Interaction, source: app_commands.Choice[str]):
        async def f(): return setter(it, receive_source=source.value)
        await guarded(it, True, f)

    @group.command(name="report", description="Daily health line and link up/down notices in this channel")
    @app_commands.choices(state=ONOFF)
    async def report(it: discord.Interaction, state: app_commands.Choice[int]):
        async def f(): return setter(it, report=bool(state.value))
        await guarded(it, True, f)

    @group.command(name="footer", description="Hops, SNR and route under Public messages in this channel")
    @app_commands.choices(state=ONOFF)
    async def footer(it: discord.Interaction, state: app_commands.Choice[int]):
        async def f(): return setter(it, footer=bool(state.value))
        await guarded(it, True, f)

    @group.command(name="health", description="Node health now (admin channels only)")
    async def health(it: discord.Interaction):
        async def f():
            b = bridge()
            b.require_admin(it.channel_id)
            if not b.health:
                raise BridgeError("Health monitor not running.")
            live = None
            if b.node.connected:
                try:
                    live = {"battery_mv": await b.node.battery(), **await b.node.stats()}  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    live = None
            return b.health.build_report(live=live)
        await guarded(it, False, f, slow=True)

    @group.command(name="where", description="Where are the kids? Latest GPS fix per member (admin channels only)")
    @app_commands.describe(name="one member's name (default: everyone)")
    async def where(it: discord.Interaction, name: str | None = None):
        async def f(): return await bridge().where(it.channel_id, name)
        await guarded(it, False, f, slow=True)

    @group.command(name="cli", description="Run a node CLI command (admin channels only)")
    @app_commands.describe(command="e.g. get repeat")
    async def cli(it: discord.Interaction, command: str):
        async def f():
            out = await bridge().run_cli(it.channel_id, command)
            return f"`{command[:100]}`\n```\n{out[:1700]}\n```"
        await guarded(it, False, f, slow=True)

    alias = app_commands.Group(name="alias", description="Mesh names (aliases) of Discord users", parent=group)

    @alias.command(name="set", description="Set the name a Discord user posts under on the mesh")
    @app_commands.describe(user="the Discord user", name="mesh name (no ':', at most 31 bytes)")
    async def alias_set(it: discord.Interaction, user: discord.User, name: str):
        async def f(): return bridge().alias_set(user.id, name, it.user.id)
        await guarded(it, True, f)

    @alias.command(name="list", description="List the mesh aliases of Discord users")
    async def alias_list(it: discord.Interaction):
        async def f(): return bridge().alias_list()
        await guarded(it, False, f)

    @alias.command(name="del", description="Remove a user's alias (their display name is used again)")
    @app_commands.describe(user="the Discord user")
    async def alias_del(it: discord.Interaction, user: discord.User):
        async def f(): return bridge().alias_del(user.id, it.user.id)
        await guarded(it, True, f)

    return group
