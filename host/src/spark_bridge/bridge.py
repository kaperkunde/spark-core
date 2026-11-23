"""Bridge logic: per-channel composable behaviour, with no discord.py import.

Per Discord channel (stored in SQLite, see store.ChannelConfig):
  admin    on/off      allows `/spark cli` and `/spark health` here
  send     none|public|room|both      Discord -> mesh; prefix on/off; rate limit
                                      (public/both needs an explicit confirmation)
  receive  none|public|room|both      mesh -> Discord
  report   on/off      daily health line and link up/down notices here
  footer   on/off      hops, SNR and route under Public messages (default on)
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import Config
from .nodelink import ChannelMsg, LinkError, RoomPost
from .store import RATE_MODES, RECEIVE_SOURCES, SEND_TARGETS, ChannelConfig, Store
from .textlimits import (ROOM_POST_MAX, clean, public_budget, sanitize_sender, split_author, tag,
                         text_hash, truncate_utf8)

log = logging.getLogger("spark_bridge.bridge")

REACT_TRUNCATED = "✂️"  # scissors
REACT_LIMITED = "⏳"  # hourglass
REACT_DOWN = "\U0001f50c"  # plug
REACT_FAILED = "❌"  # cross

RECENT_SECONDS = 120
WHERE_MAX_PAGES = 10
_WHERE_FIX = re.compile(r"^(?P<name>.+) (?P<age>\S+) (?P<lat>-?\d+\.\d+),(?P<lon>-?\d+\.\d+) s(?P<sats>\d+)$")
_WHERE_NEVER = re.compile(r"^(?P<name>.+) never$")
_WHERE_MORE = re.compile(r"^more: where (?P<start>\d+)$")
_MEMBER_LINE = re.compile(r"^(?P<prefix>[0-9A-Fa-f]{8}) (?P<name>.+?)(?P<admin>\*?) (?P<age>\S+)$")
_MEMBERS_MORE = re.compile(r"^more: members (?P<start>\d+)$")
MEMBERS_REFRESH_SECONDS = 300   # at most this often when an unknown author shows up


def parse_members(text: str) -> tuple[list[tuple[str, str, bool]], int | None]:
    """Parse one reply of the node's `members` CLI command (SparkNode1): (prefix, name, admin) per
    member with a name, and the start index of the next page. Unrecognised lines (an older
    firmware's "unknown command") are ignored."""
    out: list[tuple[str, str, bool]] = []
    more = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("> "):
            line = line[2:].strip()
        if m := _MEMBERS_MORE.match(line):
            more = int(m["start"])
        elif m := _MEMBER_LINE.match(line):
            if m["name"] != "-":
                out.append((m["prefix"].lower(), m["name"], bool(m["admin"])))
    return out, more


@dataclass
class WhereFix:
    name: str
    age: str            # as the node prints it: "45s", "12m", "3h", "4d", "never"
    lat: float | None = None
    lon: float | None = None
    sats: int = 0

    @property
    def maps_url(self) -> str | None:
        if self.lat is None or self.lon is None:
            return None
        return f"https://maps.google.com/?q={self.lat:.6f},{self.lon:.6f}"


def parse_where(text: str) -> tuple[list[WhereFix], int | None]:
    """Parse one reply of the node's `where` CLI command (SparkNode1). Returns the fixes and the start
    index of the next page, if any. Lines are parsed from the right: names may contain spaces."""
    fixes: list[WhereFix] = []
    more = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("> "):
            line = line[2:].strip()
        if not line or line == "no locations":
            continue
        if line.startswith("Err"):
            raise BridgeError(line)
        if m := _WHERE_MORE.match(line):
            more = int(m["start"])
        elif m := _WHERE_FIX.match(line):
            fixes.append(WhereFix(m["name"], m["age"], float(m["lat"]), float(m["lon"]), int(m["sats"])))
        elif m := _WHERE_NEVER.match(line):
            fixes.append(WhereFix(m["name"], "never"))
    return fixes, more


def format_where(fixes: list[WhereFix]) -> str:
    if not fixes:
        return "No locations known yet."
    out = []
    for f in fixes:
        if f.maps_url is None:
            out.append(f"**{f.name}**: no location yet")
        else:
            ago = "just now" if f.age.endswith("s") else f"{f.age} ago"
            sats = f", {f.sats} satellites" if f.sats else ""
            out.append(f"**{f.name}**: {ago}{sats}\n<{f.maps_url}>")
    return "\n".join(out)
NOTICE_PAIR_SECONDS = 600
META_DOWN_PENDING = "down_notice_pending"
META_LAST_PAIR = "last_notice_pair"
AUDIT_PUBLIC_ON = "send public on"
AUDIT_PUBLIC_OFF = "send public off"
DEFAULT_SENDER = "Discord"   # when a display name has nothing usable left after sanitizing

PUBLIC_SEND_WARNING = (
    "**Warning: Discord to MeshCore Public**\n"
    "- Sending to Public from Discord is designed for single-user convenience, "
    "not for mass sending or bots.\n"
    "- Anyone who can write in this Discord channel will be able to send to Public "
    "using your station's credentials and identity.\n"
    "- Don't enable it on public or shared Discord channels.\n"
    "The rate limits stay in force. Who enabled it, and when, is logged.")
DANGEROUS_CLI = ("password", "set password", "set guest.password", "set prv.key", "get prv.key",
                 "erase", "start ota", "set ble.pin", "dfu")


LOG_TEXT_MAX = 40


def shorten(text: str, n: int = LOG_TEXT_MAX) -> str:
    """Public text for a log line: one line, at most n characters."""
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


@dataclass
class Inbound:
    """A human message in a Discord channel."""
    guild_id: int
    channel_id: int
    message_id: int
    user_id: int
    author: str  # display name
    text: str


class ChatPort(Protocol):
    async def send_as(self, channel_id: int, author: str, text: str) -> None: ...
    async def react(self, channel_id: int, message_id: int, emoji: str) -> None: ...
    async def notice(self, channel_id: int, text: str) -> None: ...


class NodePort(Protocol):
    connected: bool
    node_name: str
    version: str

    @property
    def own_author(self) -> str: ...
    async def send_public(self, text: str, sender: str | None = None) -> None: ...
    async def post_room(self, text: str) -> None: ...
    async def cli(self, command: str) -> str: ...
    async def drain(self) -> int: ...


class SlidingWindow:
    """N events per 60 s per key."""

    def __init__(self, per_minute: int, clock: Callable[[], float]):
        self.n, self.clock = per_minute, clock
        self.hits: dict[Any, deque[float]] = defaultdict(deque)

    def _trim(self, key) -> deque:
        q = self.hits[key]
        now = self.clock()
        while q and q[0] <= now - 60:
            q.popleft()
        return q

    def allowed(self, key, n: int | None = None) -> bool:
        return len(self._trim(key)) < (self.n if n is None else n)

    def hit(self, key) -> None:
        self._trim(key).append(self.clock())


class TokenBucket:
    def __init__(self, per_minute: int, burst: int, clock: Callable[[], float]):
        self.rate, self.burst, self.clock = per_minute / 60.0, float(burst), clock
        self.tokens, self.last = float(burst), clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def available(self) -> bool:
        if self.rate <= 0:
            return True  # disabled
        self._refill()
        return self.tokens >= 1

    def take(self) -> None:
        if self.rate > 0:
            self._refill()
            self.tokens -= 1


class BridgeError(Exception):
    """A user-facing configuration error."""


class NeedsConfirmation(BridgeError):
    """Enabling Discord -> Public send needs an explicit confirmation (the warning is the text)."""

    def __init__(self, target: str):
        super().__init__(f"{PUBLIC_SEND_WARNING}\n\nTo enable `send {target}`, press **Confirm** or run "
                         f"`/spark send target:{target} confirm:True`.")
        self.target = target


class ChatClosed(Exception):
    """The chat client is shutting down (e.g. aiohttp "Session is closed"): drop quietly."""


class ChannelGone(Exception):
    """The Discord channel no longer exists (404 Unknown Channel, code 10003)."""

    def __init__(self, channel_id: int):
        super().__init__(f"channel {channel_id} no longer exists")
        self.channel_id = channel_id


class Bridge:
    def __init__(self, store: Store, node: NodePort, chat: ChatPort, cfg: Config,
                 clock: Callable[[], float] = time.time):
        self.store, self.node, self.chat, self.cfg, self.clock = store, node, chat, cfg, clock
        self.health = None  # set by __main__ (HealthMonitor), optional
        self.observer = None  # set by __main__ (observer.Observer), optional
        self._suppress = False
        self._recent: dict[str, float] = {}
        self._window = SlidingWindow(0, clock)
        lim = cfg.limits
        self._public_bucket = TokenBucket(lim.public_per_minute, lim.public_burst, clock)
        self._last_pair = -1e12
        self._members_refreshed = -1e12
        # "down notice posted, up notice still owed" lives in the meta table so it survives
        # process restarts (the next process's first link-up posts the "up again" notice).
        # The 10-minute pair limit also persists, so a restart cannot cause a down-notice burst.
        try:
            self._last_pair = float(self.store.get_meta(META_LAST_PAIR) or -1e12)
        except ValueError:
            pass

    # ------------------------------------------------------------------ link events
    async def on_link_up(self) -> None:
        self.store.add_link_event(int(self.clock()), True)
        first = self.store.get_meta("initialized") is None
        self._suppress = first  # no backfill of old posts on the first run
        await self.refresh_member_names()
        try:
            n = await self.node.drain()
            log.info("initial sync: %d messages%s", n, " (first run, not forwarded)" if first else "")
        finally:
            self._suppress = False
        if first:
            self.store.set_meta("initialized", str(int(self.clock())))
        if self.store.get_meta(META_DOWN_PENDING) is not None:
            self.store.delete_meta(META_DOWN_PENDING)
            await self._notify(f"Link to node **{self.node.node_name}** is up again.")

    async def on_link_down(self) -> None:
        self.store.add_link_event(int(self.clock()), False)
        now = self.clock()
        if now - self._last_pair < NOTICE_PAIR_SECONDS:
            return  # at most one down/up pair per 10 minutes
        self._last_pair = now
        self.store.set_meta(META_LAST_PAIR, str(now))
        self.store.set_meta(META_DOWN_PENDING, str(int(now)))
        await self._notify("Link to the node is **down**; reconnecting.")

    def forget_channel(self, channel_id: int, why: str = "deleted in Discord") -> bool:
        """Drop a channel's row once its Discord channel is gone. Logs once; later calls are no-ops."""
        if self.store.delete_channel(channel_id):
            log.warning("channel %s removed from the bridge: %s", channel_id, why)
            return True
        return False

    async def _notify(self, text: str) -> None:
        for ch in self.store.all_channels():
            if ch.report:
                try:
                    await self.chat.notice(ch.channel_id, text)
                except ChatClosed:
                    log.info("Discord is closed; notice not posted: %s", text)
                    return
                except ChannelGone:
                    self.forget_channel(ch.channel_id, "Unknown Channel (404)")
                except Exception:  # noqa: BLE001
                    log.exception("notice to %s failed", ch.channel_id)

    async def on_advert(self, pubkey: str, name: str, adv_type: int = 0) -> None:
        self.store.set_name(pubkey[:8], name, adv_type or None)

    async def on_poll(self, sample: dict[str, Any]) -> None:
        if self.health:
            self.health.record(sample)
        if self.observer:
            self.observer.record_sample(sample)

    # ------------------------------------------------------------------ mesh -> Discord
    def _remember(self, text: str) -> None:
        now = self.clock()
        self._recent = {k: v for k, v in self._recent.items() if v > now}
        self._recent[text_hash(text)] = now + RECENT_SECONDS

    def _is_recent(self, text: str) -> bool:
        return self._recent.get(text_hash(text), 0) > self.clock()

    def route_names(self, msg: ChannelMsg) -> list[str]:
        """The route as repeater names where known (hash prefix match), else hex."""
        return [self.store.hop_name(h) or h for h in msg.hop_hashes()]

    def footer(self, msg: ChannelMsg) -> str:
        """Compact line for Discord: `2 hops · SNR 5.0 dB · via A → B`, or `direct · SNR ...`."""
        if msg.hops is None:
            return ""
        parts = ["direct" if msg.direct or msg.hops == 0 else f"{msg.hops} hop{'s' if msg.hops != 1 else ''}"]
        if msg.snr is not None:
            parts.append(f"SNR {msg.snr:.1f} dB")
        names = self.route_names(msg)
        if names:
            more = "" if msg.path_complete else " → …"
            parts.append("via " + " → ".join(names) + more)
        return " · ".join(parts)

    async def on_channel_msg(self, msg: ChannelMsg) -> None:
        if msg.channel != 0:
            return
        h = text_hash(msg.text)
        if self.store.seen("c", msg.ts, "", h):
            return
        route = ",".join(msg.hop_hashes()) if msg.path is not None else None
        self.store.mark_seen("c", msg.ts, "", h, snr=msg.snr, rssi=msg.rssi,
                             hops=-1 if msg.direct else msg.hops, path=route)
        author, body = split_author(msg.text)
        log.info("public from %s: %s, rssi %s, route %s: %s", author, self.footer(msg).split(" · via")[0] or "no meta",
                 msg.rssi if msg.rssi is not None else "-", route if route is not None else "-", shorten(body))
        if self._suppress:
            return
        if self._is_recent(body) or self._is_recent(msg.text):
            return
        await self._fan_out("public", author, body, footer=self.footer(msg))

    async def on_room_post(self, post: RoomPost) -> None:
        h = text_hash(post.text)
        if self.store.seen("r", post.ts, post.author, h):
            return
        self.store.mark_seen("r", post.ts, post.author, h)
        if self._suppress:
            return
        own = post.author == self.node.own_author
        # CONTRACT WORKAROUND (see README, "contract notes"): after every APP_START the node
        # replays stored posts, including the ones this bridge made itself (authored by the
        # room, with a room-clock timestamp we cannot predict). The contract only promises "no
        # echo" at send time, so remember what we sent and drop matching room-authored posts.
        if own and (self.store.was_sent(h) or self._is_recent(post.text)):
            return
        if own:
            author = self.node.node_name or "node"
        else:
            author = self.store.get_name(post.author)
            if author is None and self.clock() - self._members_refreshed >= MEMBERS_REFRESH_SECONDS:
                await self.refresh_member_names()   # a new member: ask the room for its name
                author = self.store.get_name(post.author)
            author = author or f"node {post.author}"
        await self._fan_out("room", author, post.text)

    async def _fan_out(self, source: str, author: str, text: str, skip_channel: int | None = None,
                       footer: str = "") -> None:
        for ch in self.store.all_channels():
            if ch.channel_id == skip_channel or not ch.receives(source):
                continue
            out = f"{text}\n-# {footer}" if footer and ch.footer else text   # -# = Discord subtext
            try:
                await self.chat.send_as(ch.channel_id, author, out)
            except ChatClosed:
                log.info("Discord is closed; message from %s not delivered", author)
                return
            except ChannelGone:
                self.forget_channel(ch.channel_id, "Unknown Channel (404)")
            except Exception:  # noqa: BLE001
                log.exception("delivery to channel %s failed", ch.channel_id)

    # ------------------------------------------------------------------ Discord -> mesh
    async def on_discord_message(self, m: Inbound) -> None:
        ch = self.store.get_channel(m.channel_id)
        if ch is None or ch.send_target == "none":
            return
        body = clean(m.text)
        if not body:
            return

        async def react(emoji: str) -> None:
            try:
                await self.chat.react(m.channel_id, m.message_id, emoji)
            except Exception:  # noqa: BLE001
                log.debug("reaction failed", exc_info=True)

        if not self.node.connected:
            await react(REACT_DOWN)
            return
        key = m.channel_id if ch.rate_mode == "channel" else (m.channel_id, m.user_id)
        if ch.rate_mode != "none" and not self._window.allowed(key, ch.rate_per_minute):
            await react(REACT_LIMITED)
            return
        if ch.sends_to("public") and not self._public_bucket.available():
            await react(REACT_LIMITED)
            return

        sent_to: list[str] = []
        truncated = failed = False
        name = self.sender_name(m.user_id, m.author)
        full = tag(name, body, ch.prefix)
        if ch.sends_to("public"):
            # With prefix on, a SparkNode1.2+ node sends "<alias>: <text>" itself (sender name in
            # the frame), so there is no "<node>: [alias] text" double name. Older nodes: tag.
            use_sender = ch.prefix and getattr(self.node, "supports_sender", False)
            if use_sender:
                text, cut = truncate_utf8(body, public_budget(name))
            else:
                text, cut = truncate_utf8(full, public_budget(self.node.node_name))
            truncated |= cut
            try:
                self._remember(text)
                self._public_bucket.take()
                if use_sender:
                    await self.node.send_public(text, sender=name)
                else:
                    await self.node.send_public(text)
                sent_to.append("public")
                log.info("discord -> public: channel %s, sender %s, %d bytes%s: %s", m.channel_id, name,
                         len(text.encode()), " (cut)" if cut else "", shorten(text))
            except LinkError as exc:
                log.warning("public send failed: %s", exc)
                failed = True
        if ch.sends_to("room"):
            text, cut = truncate_utf8(full, ROOM_POST_MAX)
            truncated |= cut
            try:
                self._remember(text)
                self.store.add_sent(text_hash(text), self.clock())
                await self.node.post_room(text)
                sent_to.append("room")
                # never the text of a room post (the room is private)
                log.info("discord -> room: channel %s, sender %s, %d bytes%s", m.channel_id, name,
                         len(text.encode()), " (cut)" if cut else "")
            except LinkError as exc:
                log.warning("room post failed: %s", exc)
                failed = True
        if sent_to and ch.rate_mode != "none":
            self._window.hit(key)
        if truncated:
            await react(REACT_TRUNCATED)
        if failed:
            await react(REACT_FAILED)
        # The node does not echo our own traffic, so show it to the other channels that
        # receive the same source (e.g. a read-only "everything" channel).
        for target in sent_to:
            await self._fan_out(target, m.author, body, skip_channel=m.channel_id)

    # ------------------------------------------------------------------ commands
    def configure(self, channel_id: int, guild_id: int, user_id: int | None = None, *,
                  confirm: bool = False, **changes) -> ChannelConfig:
        """Validate and persist changes to a channel's behaviour. Setting send to public or both
        raises NeedsConfirmation unless `confirm` is true (or the channel already has that target)."""
        if "send_target" in changes and changes["send_target"] not in SEND_TARGETS:
            raise BridgeError(f"send must be one of {', '.join(SEND_TARGETS)}")
        if "receive_source" in changes and changes["receive_source"] not in RECEIVE_SOURCES:
            raise BridgeError(f"receive must be one of {', '.join(RECEIVE_SOURCES)}")
        if "rate_mode" in changes and changes["rate_mode"] not in RATE_MODES:
            raise BridgeError(f"ratelimit must be one of {', '.join(RATE_MODES)}")
        n = changes.get("rate_per_minute")
        if n is not None and not 1 <= n <= 600:
            raise BridgeError("per_minute must be between 1 and 600")
        if changes.get("rate_mode") in ("channel", "user") and n is None:
            changes.pop("rate_per_minute", None)  # keep the stored value
        cur = self.store.get_channel(channel_id)
        old_target = cur.send_target if cur else "none"
        new_target = changes.get("send_target", old_target)
        public_before = old_target in ("public", "both")
        public_after = new_target in ("public", "both")
        if public_after and new_target != old_target and not confirm:
            raise NeedsConfirmation(new_target)
        if changes.get("admin") and not (cur and cur.admin) and "report" not in changes:
            changes["report"] = True  # new admin channels get the daily line and link notices
        new = self.store.update_channel(channel_id, guild_id, self.cfg.limits.default_per_minute,
                                        user_id, **changes)
        if public_after and new_target != old_target:
            self.store.add_audit(channel_id, user_id, AUDIT_PUBLIC_ON, self.clock())
            log.warning("Discord -> Public send enabled (send %s) in channel %s (guild %s) by user %s",
                        new_target, channel_id, guild_id, user_id)
        elif public_before and not public_after:
            self.store.add_audit(channel_id, user_id, AUDIT_PUBLIC_OFF, self.clock())
            log.info("Discord -> Public send disabled in channel %s by user %s", channel_id, user_id)
        return new

    def describe(self, ch: ChannelConfig | None, channel_id: int) -> str:
        ch = ch or ChannelConfig(channel_id=channel_id, rate_per_minute=self.cfg.limits.default_per_minute)
        onoff = lambda b: "on" if b else "off"  # noqa: E731
        if ch.send_target == "none":
            send = "none"
        else:
            rate = ("no limit" if ch.rate_mode == "none"
                    else f"{ch.rate_per_minute}/min per {ch.rate_mode}")
            send = f"{ch.send_target}, prefix {onoff(ch.prefix)}, rate limit {rate}"
            if ch.sends_to("public"):
                who = self.store.last_audit(ch.channel_id, AUDIT_PUBLIC_ON)
                if who:
                    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(who[0]))
                    by = f"<@{who[1]}>" if who[1] else "unknown"
                    send += f" (Public enabled by {by}, {when})"
        link = (f"up ({self.node.node_name} {self.node.version})" if self.node.connected else "down")
        return (f"**Channel behaviour**\n"
                f"- admin: {onoff(ch.admin)}\n"
                f"- send (Discord to mesh): {send}\n"
                f"- receive (mesh to Discord): {ch.receive_source}\n"
                f"- daily report and link notices: {onoff(ch.report)}\n"
                f"- footer under Public messages (hops, SNR, route): {onoff(ch.footer)}\n"
                f"{self.observer_line()}"
                f"Link to node: {link}")

    def observer_line(self) -> str:
        obs = getattr(self, "observer", None)
        return f"MQTT observer: {obs.summary()}\n" if obs is not None else ""

    def status(self, channel_id: int) -> str:
        return self.describe(self.store.get_channel(channel_id), channel_id)

    # ------------------------------------------------------------------ aliases
    def sender_name(self, user_id: int, display_name: str) -> str:
        """The name a Discord user has on the mesh: the alias, else the sanitized display name."""
        alias = self.store.get_alias(user_id)
        if alias:
            return alias
        return sanitize_sender(display_name) or DEFAULT_SENDER

    def alias_set(self, user_id: int, name: str, by: int | None = None) -> str:
        clean_name = sanitize_sender(name)
        if not clean_name:
            raise BridgeError("That alias is empty after removing ':' and control characters.")
        owner = self.store.alias_owner(clean_name)
        if owner is not None and owner != user_id:
            raise BridgeError(f"`{clean_name}` is already the alias of <@{owner}>.")
        self.store.set_alias(user_id, clean_name, by)
        log.info("alias for user %s set to %r by %s", user_id, clean_name, by)
        note = "" if clean_name == name.strip() else " (cleaned up: no ':' or line breaks, at most 31 bytes)"
        return f"<@{user_id}> now posts to the mesh as **{clean_name}**{note}."

    def alias_del(self, user_id: int, by: int | None = None) -> str:
        if not self.store.delete_alias(user_id):
            raise BridgeError(f"<@{user_id}> has no alias.")
        log.info("alias for user %s removed by %s", user_id, by)
        return f"Alias of <@{user_id}> removed: their Discord display name is used again."

    def alias_list(self) -> str:
        rows = self.store.all_aliases()
        if not rows:
            return "No aliases set. Without one, a user posts to the mesh under their Discord display name."
        return "**Mesh aliases**\n" + "\n".join(f"- <@{uid}>: {name}" for uid, name in rows)

    def cli_allowed(self, command: str) -> bool:
        norm = " ".join(command.lower().split())
        if self.cfg.allow_dangerous_cli:
            return True
        return not any(norm == d or norm.startswith(d + " ") for d in DANGEROUS_CLI)

    async def run_cli(self, channel_id: int, command: str) -> str:
        ch = self.store.get_channel(channel_id)
        if ch is None or not ch.admin:
            raise BridgeError("This channel is not an admin channel. A user with Manage Channels can run `/spark admin on`.")
        command = command.strip()
        if not command:
            raise BridgeError("Empty command.")
        if not self.cli_allowed(command):
            raise BridgeError("That command is blocked by the bridge (set allow_dangerous_cli = true to override).")
        if not self.node.connected:
            raise BridgeError("Link to the node is down.")
        try:
            reply = await self.node.cli(command)
        except LinkError as exc:
            raise BridgeError(f"Node error: {exc}") from exc
        return reply or "(no reply)"

    async def refresh_member_names(self) -> int:
        """Learn names from the room's member directory (`members`, SparkNode1): a kid's device
        names itself there even when its advert never reaches the room. Returns names stored."""
        self._members_refreshed = self.clock()
        if not self.node.connected:
            return 0
        n = 0
        start: int | None = 0
        try:
            for _ in range(WHERE_MAX_PAGES):
                found, start = parse_members(await self.node.cli("members" if not start else f"members {start}"))
                for prefix, name, _admin in found:
                    self.store.set_name(prefix, name, 1)   # members are clients, not relays
                    n += 1
                if start is None:
                    break
        except LinkError as exc:
            log.info("members refresh failed: %s", exc)
        return n

    async def where(self, channel_id: int, name: str | None = None) -> str:
        """Latest GPS fix of every member (or one), from the room's `where` command. Admin
        channels only: locations of kids must never reach other channels."""
        self.require_admin(channel_id)
        if not self.node.connected:
            raise BridgeError("Link to the node is down.")
        name = " ".join((name or "").split())[:15]
        fixes: list[WhereFix] = []
        try:
            if name:
                found, _ = parse_where(await self.node.cli(f"where {name}"))
                fixes.extend(found)
            else:
                start: int | None = 0
                for _ in range(WHERE_MAX_PAGES):
                    found, start = parse_where(await self.node.cli("where" if not start else f"where {start}"))
                    fixes.extend(found)
                    if start is None:
                        break
        except LinkError as exc:
            raise BridgeError(f"Node error: {exc}") from exc
        return format_where(fixes)

    def require_admin(self, channel_id: int) -> None:
        ch = self.store.get_channel(channel_id)
        if ch is None or not ch.admin:
            raise BridgeError("This channel is not an admin channel.")
