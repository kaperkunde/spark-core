"""The only module that talks to the `meshcore` library.

Lib calls used per contract command (meshcore 2.3.19), no raw frames were needed:

  APP_START            commands.send_appstart()            (also done by MeshCore.connect())
  DEVICE_QUERY         commands.send_device_query()
  SET_DEVICE_TIME      commands.set_time(epoch)
  GET_BATT_AND_STORAGE commands.get_bat()
  GET_STATS 0/1        commands.get_stats_core() / get_stats_radio()
  SEND_CHANNEL_TXT_MSG commands.send_chan_msg(0, text, timestamp)
  SEND_TXT_MSG (room)  commands.send_msg(<own 6-byte prefix>, text, timestamp)
  RUN_CLI_COMMAND      commands.run_cli_command(cmd)
  SYNC_NEXT_MESSAGE    commands.get_msg()   (reply events used directly, no subscriptions)
  GET_CONTACTS         commands.get_contacts()
  pushes               subscribe(MESSAGES_WAITING | NEW_CONTACT | DISCONNECTED) with sync callbacks

Every command goes through one lock: the lib matches replies by event type and a shared ERROR
event, so overlapping commands could be answered with each other's errors.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

from meshcore import EventType, MeshCore

from .config import NodeConfig

log = logging.getLogger("spark_bridge.node")


class LinkError(Exception):
    pass


class LinkGaveUp(Exception):
    """No link for `give_up_minutes`: the process should exit so a supervisor restarts it."""


NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"  # MeshCore BLE UART service


@dataclass
class RoomPost:
    ts: int
    author: str  # 4-byte author prefix, hex
    text: str


@dataclass
class ChannelMsg:
    ts: int
    text: str  # "<sender>: <message>"
    channel: int = 0


class Handlers(Protocol):
    async def on_link_up(self) -> None: ...
    async def on_link_down(self) -> None: ...
    async def on_room_post(self, post: RoomPost) -> None: ...
    async def on_channel_msg(self, msg: ChannelMsg) -> None: ...
    async def on_advert(self, pubkey: str, name: str, adv_type: int) -> None: ...
    async def on_poll(self, sample: dict[str, Any]) -> None: ...


class NodeLink:
    def __init__(self, cfg: NodeConfig, handlers: Handlers | None = None, *, clock=time.time,
                 sleep=asyncio.sleep, rng: random.Random | None = None, monotonic=time.monotonic,
                 lock=None, pause_grace: float = 5.0):
        self.cfg = cfg
        self.handlers = handlers
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._monotonic = monotonic
        self._bleak = None  # bleak client of the current BLE session, so close() can force it shut
        self._fail_streak = 0  # consecutive failed connection attempts
        self._down_since: float | None = None  # monotonic time the link was last lost
        self.mc: MeshCore | None = None
        self.connected = False
        self.node_name = ""
        self.pubkey = ""  # hex, 32 bytes
        self.version = ""
        self.sessions = 0
        self._lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        self._disconnected = asyncio.Event()
        self._waiting = asyncio.Event()
        self._stop = asyncio.Event()
        # Link lock (lock.LinkLock): held while this process may use the link. request_pause()
        # (SIGUSR1, from `spark-bridge update`) closes the link and hands the lock over.
        self.lock = lock
        self.pause_grace = pause_grace
        self.paused = False
        self._pause = asyncio.Event()

    # --- identity ------------------------------------------------------------
    @property
    def own_prefix(self) -> bytes:
        return bytes.fromhex(self.pubkey)[:6]

    @property
    def own_author(self) -> str:
        """The author prefix (4 bytes, hex) the node uses for posts made by the room itself."""
        return self.pubkey[:8]

    # --- connection ----------------------------------------------------------
    async def _create(self) -> MeshCore:
        c = self.cfg
        kw = dict(default_timeout=c.command_timeout, auto_reconnect=False)
        if c.transport == "tcp":
            mc = await MeshCore.create_tcp(c.tcp_host, c.tcp_port, **kw)
        elif c.transport == "serial":
            mc = await MeshCore.create_serial(c.serial_port, **kw)
        else:
            # No PIN: pairing is done once at OS level. An address without ':' is matched as a
            # substring of the BLE name ("SparkNode" matches "MeshCore-SparkNode").
            dev = None
            if self._fail_streak >= c.ble_direct_after:
                # A peripheral that is already connected does not advertise, so a scan cannot
                # see it (stale CoreBluetooth/BlueZ connection). Ask the OS for it instead.
                dev = await self._find_connected_ble()
                if dev is not None:
                    log.info("scans found nothing; using the OS-connected peripheral %s", dev)
            if dev is not None:
                mc = await MeshCore.create_ble(device=dev, **kw)
            else:
                mc = await MeshCore.create_ble(address=c.ble_address or c.ble_name, **kw)
        if mc is None:
            raise LinkError("no response from node")
        self._bleak = getattr(getattr(mc, "connection_manager", None), "connection", None)
        self._bleak = getattr(self._bleak, "client", None)
        return mc

    async def _find_connected_ble(self):
        """The node's BLEDevice if the OS reports it as already connected, else None
        (bleak >= 3.1 BleakAdapter; works on macOS CoreBluetooth and Linux BlueZ)."""
        c = self.cfg
        try:
            from bleak import BleakAdapter

            adapter = await asyncio.wait_for(BleakAdapter.get(), 10)
            devices = await asyncio.wait_for(adapter.get_connected_devices([NUS_SERVICE_UUID]), 10)
        except Exception as exc:  # noqa: BLE001
            log.debug("connected-device lookup failed: %s", exc)
            return None
        for d in devices:
            name = (d.name or "")
            if c.ble_address and ":" in c.ble_address:
                if (d.address or "").lower() == c.ble_address.lower():
                    return d
            elif name.startswith("MeshCore") and (not c.ble_name or c.ble_name in name):
                return d
        return None

    async def connect(self) -> None:
        """Connect and run the handshake, all under `connect_timeout`; a timeout is a failed attempt."""
        try:
            await asyncio.wait_for(self._connect(), self.cfg.connect_timeout)
        except asyncio.TimeoutError:
            log.warning("connect timed out after %.0f s", self.cfg.connect_timeout)
            await self._close_quietly()
            raise LinkError(f"connect timed out after {self.cfg.connect_timeout:.0f} s") from None

    async def _close_quietly(self) -> None:
        try:
            await self.close()
        except Exception:  # noqa: BLE001
            log.debug("close after timeout failed", exc_info=True)

    async def _connect(self) -> None:
        """APP_START, DEVICE_QUERY, SET_DEVICE_TIME, contacts."""
        self._disconnected.clear()
        self._waiting.clear()
        mc = await self._create()
        self.mc = mc
        try:
            mc.subscribe(EventType.DISCONNECTED, lambda ev: self._disconnected.set())
            mc.subscribe(EventType.MESSAGES_WAITING, lambda ev: self._waiting.set())
            mc.subscribe(EventType.NEW_CONTACT, self._advert_cb)
            ev = await self._cmd(mc.commands.send_appstart(timeout=self.cfg.command_timeout))
            self.pubkey = ev.payload.get("public_key", "")
            self.node_name = ev.payload.get("name", "") or self.node_name
            ev = await self._cmd(mc.commands.send_device_query())
            self.version = ev.payload.get("ver", "")
            if "spark" not in self.version.lower():
                log.warning("node version %r does not contain 'spark': is this the HostLink firmware?",
                            self.version)
            try:
                await self._cmd(mc.commands.set_time(int(self._clock())))
            except LinkError as exc:
                log.warning("SET_DEVICE_TIME refused (%s); node clock is ahead of this host", exc)
            await self._load_contacts()
        except BaseException:
            await self.close()
            raise
        self.connected = True
        self.sessions += 1
        log.info("connected to %s (%s)", self.node_name, self.version)

    async def close(self) -> None:
        """Tear the session down completely: the MeshCore object AND its bleak client.

        meshcore forgets its bleak client when bleak reports a disconnect, and skips it when a
        write timed out; a client that is still connected at OS level keeps the peripheral
        owned by this process (it then never advertises again). So disconnect it explicitly.
        """
        self.connected = False
        mc, self.mc = self.mc, None
        bleak, self._bleak = self._bleak, None
        if mc is not None:
            try:
                await asyncio.wait_for(mc.disconnect(), 10)
            except BaseException as exc:  # noqa: BLE001
                log.debug("disconnect: %s", exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
        if bleak is not None:
            try:
                if bleak.is_connected:
                    await asyncio.wait_for(bleak.disconnect(), 10)
            except Exception as exc:  # noqa: BLE001
                log.debug("bleak disconnect: %s", exc)
        del mc, bleak
        gc.collect()

    def _advert_cb(self, ev) -> None:  # sync: runs inline in the dispatcher
        p = ev.payload
        if self.handlers and p.get("adv_name"):
            asyncio.get_running_loop().create_task(
                self.handlers.on_advert(p["public_key"], p["adv_name"], p.get("type", 0)))

    async def _load_contacts(self) -> None:
        assert self.mc
        ev = await self._cmd(self.mc.commands.get_contacts())
        if self.handlers:
            for c in (ev.payload or {}).values():
                if c.get("adv_name"):
                    await self.handlers.on_advert(c["public_key"], c["adv_name"], c.get("type", 0))

    # --- commands -------------------------------------------------------------
    async def _cmd(self, coro):
        if self._disconnected.is_set():
            coro.close()
            raise LinkError("link down")
        try:
            async with self._lock:
                ev = await asyncio.wait_for(coro, self.cfg.command_timeout * 2)
        except asyncio.TimeoutError:
            raise LinkError("command timed out") from None
        if ev is None:
            raise LinkError("no reply")
        if ev.type == EventType.ERROR:
            raise LinkError(str(ev.payload))
        return ev

    def _require(self) -> MeshCore:
        if not self.connected or self.mc is None:
            raise LinkError("link down")
        return self.mc

    async def battery(self) -> int:
        ev = await self._cmd(self._require().commands.get_bat())
        return int(ev.payload["level"])

    async def stats(self) -> dict[str, Any]:
        mc = self._require()
        core = (await self._cmd(mc.commands.get_stats_core())).payload
        radio = (await self._cmd(mc.commands.get_stats_radio())).payload
        return {**core, **radio}

    async def packet_stats(self) -> dict[str, Any]:
        return dict((await self._cmd(self._require().commands.get_stats_packets())).payload)

    async def send_public(self, text: str) -> None:
        await self._cmd(self._require().commands.send_chan_msg(0, text, int(self._clock())))

    async def post_room(self, text: str) -> None:
        mc = self._require()
        await self._cmd(mc.commands.send_msg(self.own_prefix, text, int(self._clock())))

    async def cli(self, command: str) -> str:
        ev = await self._cmd(self._require().commands.run_cli_command(command))
        return ev.payload.get("text", "")

    async def contacts(self) -> dict[str, Any]:
        ev = await self._cmd(self._require().commands.get_contacts())
        return ev.payload or {}

    # --- message sync -----------------------------------------------------------
    async def drain(self) -> int:
        """SYNC_NEXT_MESSAGE until NO_MORE_MESSAGES. Returns the number of messages handled."""
        n = 0
        async with self._drain_lock:
            mc = self._require()
            while True:
                ev = await self._cmd(mc.commands.get_msg())
                if ev.type == EventType.NO_MORE_MSGS:
                    return n
                p = ev.payload
                n += 1
                if ev.type == EventType.CONTACT_MSG_RECV:
                    if p.get("txt_type") == 2 and self.handlers:
                        await self.handlers.on_room_post(
                            RoomPost(ts=p["sender_timestamp"], author=p.get("signature", ""), text=p["text"]))
                elif ev.type == EventType.CHANNEL_MSG_RECV and self.handlers:
                    await self.handlers.on_channel_msg(
                        ChannelMsg(ts=p["sender_timestamp"], text=p["text"], channel=p["channel_idx"]))

    # --- supervisor -------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._disconnected.set()
        self._waiting.set()

    def request_pause(self) -> None:
        """Close the link and release the lock until it can be taken again (an update runs)."""
        if self.lock is None:
            log.warning("pause requested, but this process has no link lock")
            return
        log.info("pause requested: closing the link for another process")
        self._pause.set()
        self._disconnected.set()
        self._waiting.set()

    async def _hold_lock(self) -> None:
        """Release the lock if a pause was requested, then wait until it is ours (again)."""
        assert self.lock is not None
        if self._pause.is_set():
            self._pause.clear()
            self.lock.release()
            self.paused = True
            self._down_since = None  # a pause is not an outage: the watchdog must not give up
            log.info("paused: link closed, lock released")
            await self._sleep(self.pause_grace)  # let the updater take the lock
        if not self.lock.held and not self.lock.try_acquire():
            self.paused = True
            self._down_since = None
            log.info("waiting for the link lock %s (spark-bridge update running?)", self.lock.path)
            while not self._stop.is_set() and not self.lock.try_acquire():
                await self._sleep(1.0)
        if self.paused:
            log.info("link lock taken again; reconnecting")
        self.paused = False
        self._down_since = self._monotonic()

    async def run(self) -> None:
        """Connect, serve, reconnect with backoff 2, 4, 8 .. 60 s plus jitter, forever.

        A watchdog task runs beside the loop: it measures wall-clock time without a link no matter
        what the loop is awaiting (a hung BLE connect), and raises LinkGaveUp after give_up_minutes.
        """
        self._down_since = self._monotonic()
        loop_task = asyncio.ensure_future(self._run_loop())
        dog = asyncio.ensure_future(self._watchdog())
        try:
            await asyncio.wait([loop_task, dog], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (loop_task, dog):
                if not t.done():
                    t.cancel()
            await asyncio.gather(loop_task, dog, return_exceptions=True)
        if dog.done() and not dog.cancelled() and dog.exception():
            raise dog.exception()
        loop_task.result()  # LinkGaveUp from the loop, or None on stop()

    async def _watchdog(self) -> None:
        limit = self.cfg.give_up_minutes * 60
        if limit <= 0:
            await asyncio.Event().wait()
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.watchdog_seconds)
            if self._down_since is not None and self._monotonic() - self._down_since >= limit:
                log.error("no link for %.0f minutes (watchdog); exiting so the supervisor restarts the process",
                          (self._monotonic() - self._down_since) / 60)
                raise LinkGaveUp(f"no link for {limit / 60:.0f} minutes")

    async def _run_loop(self) -> None:
        delay = self.cfg.backoff_start
        while not self._stop.is_set():
            was_up = False
            if self.lock is not None and (self._pause.is_set() or not self.lock.held):
                await self._hold_lock()
                if self._stop.is_set():
                    break
                delay = self.cfg.backoff_start
            try:
                await self.connect()
                was_up = True
                delay = self.cfg.backoff_start
                self._fail_streak = 0
                self._down_since = None
                if self.handlers:
                    await self.handlers.on_link_up()
                await self._serve()
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("link error: %s", exc)
            was_connected = self.connected or was_up
            await self.close()
            self._fail_streak += 1
            if self._down_since is None:
                self._down_since = self._monotonic()
            if was_connected and self.handlers:
                try:
                    await self.handlers.on_link_down()
                except Exception:  # noqa: BLE001
                    log.exception("on_link_down failed")
            if self._stop.is_set():
                break
            if self._pause.is_set():
                continue  # no backoff, no give-up: _hold_lock() runs next
            limit = self.cfg.give_up_minutes * 60
            if limit > 0 and self._monotonic() - self._down_since >= limit:
                log.error("no link for %.0f minutes; exiting so the supervisor restarts the process",
                          (self._monotonic() - self._down_since) / 60)
                raise LinkGaveUp(f"no link for {limit / 60:.0f} minutes")
            wait = delay + self._rng.uniform(0, delay * 0.25)
            log.info("reconnecting in %.1f s", wait)
            await self._sleep(wait)
            delay = min(delay * 2, self.cfg.backoff_max)

    async def _serve(self) -> None:
        """Serve one connection: drain on pushes, poll on a fixed schedule.

        The poll deadline is absolute. It must not be re-armed by every wake-up: pushes (and a
        poll that raised) used to restart the timeout, so a steady trickle of mesh traffic
        starved the battery/health poll for hours.
        """
        loop = asyncio.get_running_loop()
        failures = 0
        next_poll = loop.time() + self.cfg.poll_seconds
        while not self._stop.is_set():
            waiter = asyncio.ensure_future(self._wait_event())
            try:
                await asyncio.wait_for(waiter, max(0.0, next_poll - loop.time()))
            except asyncio.TimeoutError:
                pass
            if self._pause.is_set():
                log.info("closing the link (pause)")
                return
            if self._disconnected.is_set():
                log.info("link lost")
                return
            if self._waiting.is_set():
                self._waiting.clear()
                try:
                    await self.drain()
                except LinkError as exc:
                    log.warning("drain failed: %s", exc)
                    failures += 1
                else:
                    failures = 0
            if loop.time() >= next_poll:
                next_poll = loop.time() + self.cfg.poll_seconds
                try:
                    sample = {"battery_mv": await self.battery(), **await self.stats()}
                except LinkError as exc:
                    failures += 1
                    log.warning("poll failed (%d/%d): %s", failures, self.cfg.poll_failures, exc)
                else:
                    failures = 0
                    if self.handlers:
                        try:
                            await self.handlers.on_poll(sample)
                        except Exception:  # noqa: BLE001
                            log.exception("on_poll failed")  # a bad sample must not end polling
            if failures >= self.cfg.poll_failures:
                log.warning("giving up on the link after %d failures", failures)
                return

    async def _wait_event(self) -> None:
        tasks = [asyncio.ensure_future(self._disconnected.wait()), asyncio.ensure_future(self._waiting.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
