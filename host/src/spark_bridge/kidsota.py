"""Bluetooth updates for SpArkKids units (T-Deck Plus) near the bridge.

The bridge keeps one signed bundle (<firmware dir>/current.zip, installed with
`spark-bridge kids-firmware install`). Every few minutes it scans for kids units of its family
that advertise a lower version code and are not snoozed, connects to one, and offers the update.
The unit shows "Update available" with Install / Later; Later snoozes offers there for 12 hours.
Install streams the image (write without response, a 16 KB window acknowledged by the unit)
and the unit checks hash and signature, switches partitions and restarts.

This link is separate from the node link: it never touches the node lock. A small lock file in
the firmware directory keeps two bridge processes from updating at the same time.

    spark-bridge kids-firmware install .pio/build/SparkKids_TDeckPlus/kids-ota.zip
    spark-bridge kids-firmware show
    spark-bridge kids-update --once          # one scan and offer, then exit (tests, cron)
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import kidsfw as kf
from .bleadapter import ADAPTER_HELP, AdapterError, bleak_args
from .config import ConfigError, check_adapter, load_env

log = logging.getLogger("spark_bridge.kidsota")

WINDOW = 16 * 1024          # unacknowledged bytes in flight (the unit buffers 32 KB)
PROGRESS_TIMEOUT = 30.0     # no ack for this long: give up
DONE_TIMEOUT = 60.0         # after the last byte: hash check, partition switch


class OtaError(Exception):
    pass


def default_fw_dir(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "kids-firmware"


@dataclass
class Unit:
    device: object          # bleak BLEDevice
    adv: kf.Advert
    rssi: int

    @property
    def address(self) -> str:
        return getattr(self.device, "address", "?")


async def scan_units(seconds: float, adapter: str = "") -> list[Unit]:
    """Kids units advertising the kids-OTA manufacturer data (any family)."""
    from bleak import BleakScanner

    found: dict[str, Unit] = {}

    def on_adv(device, adv):
        a = kf.parse_advert(adv.manufacturer_data)
        if a is not None:
            found[device.address] = Unit(device, a, adv.rssi)

    async with BleakScanner(on_adv, **bleak_args(adapter)):
        await asyncio.sleep(seconds)
    return list(found.values())


class FileLock:
    """Non-blocking exclusive lock on a file (one kids updater per machine and firmware dir)."""

    def __init__(self, path: Path):
        self.path, self._fd = path, None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class Session:
    """One connection to one unit: offer, wait for the answer, stream if accepted."""

    def __init__(self, client, bundle: kf.Bundle, *, say=log.info, prompt_wait: float = 150.0):
        self.client, self.bundle, self.say, self.prompt_wait = client, bundle, say, prompt_wait
        self.events: asyncio.Queue[kf.Event] = asyncio.Queue()
        self.acked = 0
        self.ack_event = asyncio.Event()
        self.nack: int | None = None

    def on_notify(self, _char, data: bytearray) -> None:
        ev = kf.parse_event(bytes(data))
        if ev is None:
            return
        if ev.kind == kf.NTF_ACK:
            self.acked = max(self.acked, ev.value)
            self.ack_event.set()
        elif ev.kind == kf.NTF_NACK:
            self.nack = ev.value
            self.ack_event.set()
        else:
            self.events.put_nowait(ev)

    async def next_state(self, timeout: float) -> kf.Event:
        try:
            return await asyncio.wait_for(self.events.get(), timeout)
        except asyncio.TimeoutError as exc:
            raise OtaError("no answer from the unit") from exc

    async def run(self) -> kf.Event:
        """Returns the final state event (snoozed, refused, no answer, busy, done, failed)."""
        st = kf.parse_status(bytes(await self.client.read_gatt_char(kf.CTRL_UUID)))
        if st:
            self.say(f"unit {st.label} code {st.code}, battery {st.batt_mv} mV"
                     f"{', snoozed ' + str(st.snooze_secs // 60) + ' min' if st.snooze_secs else ''}")
        await self.client.start_notify(kf.CTRL_UUID, self.on_notify)
        await self.client.write_gatt_char(kf.CTRL_UUID, self.bundle.offer(), response=True)
        ev = await self.next_state(15.0)
        if ev.state == kf.ST_PROMPT:
            self.say(f"offered {self.bundle.label}: waiting for Install / Later on the unit")
            try:
                ev = await self.next_state(self.prompt_wait)
            except OtaError:   # nobody tapped: close the prompt, try again later
                await self.client.write_gatt_char(kf.CTRL_UUID, bytes([kf.CMD_ABORT]), response=True)
                return kf.Event(kf.NTF_STATE, kf.ST_NOANSWER, kf.R_TIMEOUT)
        if ev.state != kf.ST_READY:
            return ev
        self.say("Install tapped: sending the image")
        return await self.stream()

    async def stream(self) -> kf.Event:
        image, size = self.bundle.image, self.bundle.size
        chunk = self.packet_payload()
        sent, t0, last_print = 0, time.monotonic(), 0.0
        last_progress = time.monotonic()
        while self.acked < size:
            if not self.events.empty():
                ev = self.events.get_nowait()
                if ev.state in (kf.ST_FAILED, kf.ST_REFUSED):
                    return ev
            if self.nack is not None:
                sent, self.nack = max(self.nack, self.acked), None
            while sent < size and sent - self.acked < WINDOW and self.nack is None:
                n = min(chunk, size - sent)
                await self.client.write_gatt_char(kf.DATA_UUID, kf.data_packet(sent, image[sent:sent + n]),
                                                  response=False)
                sent += n
            before = self.acked
            self.ack_event.clear()
            try:
                await asyncio.wait_for(self.ack_event.wait(), 2.0)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if self.acked > before:
                last_progress = now
            elif now - last_progress > PROGRESS_TIMEOUT:
                raise OtaError(f"no progress for {PROGRESS_TIMEOUT:.0f} s at {self.acked}/{size} bytes")
            elif now - last_progress > 5 and sent - self.acked >= WINDOW:
                sent = self.acked   # acks stalled with a full window: resend from the last ack
            if now - last_print > 5 or self.acked >= size:
                last_print = now
                rate = self.acked / max(now - t0, 0.001)
                self.say(f"sent {self.acked * 100 // size}%  {self.acked // 1024}/{size // 1024} KB  "
                         f"{rate / 1024:.1f} KB/s")
        self.say("image sent: the unit checks it and restarts")
        while True:
            ev = await self.next_state(DONE_TIMEOUT)
            if ev.state in (kf.ST_DONE, kf.ST_FAILED, kf.ST_REFUSED):
                return ev

    def packet_payload(self) -> int:
        size = 0
        try:
            ch = self.client.services.get_characteristic(kf.DATA_UUID)
            size = int(getattr(ch, "max_write_without_response_size", 0) or 0)
        except Exception:  # noqa: BLE001
            size = 0
        if size <= 0:
            size = int(getattr(self.client, "mtu_size", 23) or 23) - 3
        return max(16, min(size, 244)) - 4


async def offer_unit(unit: Unit, bundle: kf.Bundle, *, adapter: str = "", say=log.info,
                     prompt_wait: float = 150.0) -> kf.Event:
    from bleak import BleakClient

    async with BleakClient(unit.device, timeout=20.0, **bleak_args(adapter)) as client:
        return await Session(client, bundle, say=say, prompt_wait=prompt_wait).run()


def pick(units: list[Unit], bundle: kf.Bundle, policy: kf.OfferPolicy, now: float,
         say=log.debug) -> list[Unit]:
    out = []
    for u in sorted(units, key=lambda u: -u.rssi):
        ok, why = policy.should_offer(u.address, u.adv, bundle, now)
        if u.adv.family == bundle.family:
            say(f"kids unit {u.address} code {u.adv.code} rssi {u.rssi}: {why}")
        if ok:
            out.append(u)
    return out


async def scan_and_offer(bundle: kf.Bundle, policy: kf.OfferPolicy, *, adapter: str = "",
                         scan_seconds: float = 20.0, address: str = "", say=log.info,
                         prompt_wait: float = 150.0, clock=time.time) -> list[tuple[str, str]]:
    """One round. Returns [(address, outcome)] for the units it talked to."""
    units = await scan_units(scan_seconds, adapter)
    if address:
        units = [u for u in units if u.address.lower() == address.lower()]
    results = []
    for u in pick(units, bundle, policy, clock(), say=say):
        say(f"offering {bundle.label} (code {bundle.code}) to {u.address} (code {u.adv.code})")
        try:
            ev = await offer_unit(u, bundle, adapter=adapter, say=say, prompt_wait=prompt_wait)
            outcome, secs = kf.outcome_for(ev)
            say(f"{u.address}: {ev.describe()}")
        except Exception as exc:  # noqa: BLE001  (bleak raises many kinds)
            outcome, secs = "error", None
            say(f"{u.address}: {exc.__class__.__name__}: {exc}")
        nxt = policy.record(u.address, outcome, bundle, clock(), secs)
        say(f"{u.address}: {outcome}, next offer after {time.strftime('%H:%M', time.localtime(nxt))}")
        results.append((u.address, outcome))
    return results


class KidsOtaService:
    """Daemon task: scan every few minutes while the node link is up (bundle reloaded on change)."""

    def __init__(self, fw_dir: Path, *, adapter: str = "", scan_minutes: float = 5.0, node=None):
        self.fw_dir, self.adapter, self.scan_minutes, self.node = Path(fw_dir), adapter, scan_minutes, node
        self.policy = kf.OfferPolicy()
        self._bundle: kf.Bundle | None = None
        self._mtime = 0.0
        self._stop = asyncio.Event()

    def bundle(self) -> kf.Bundle | None:
        p = self.fw_dir / kf.CURRENT
        try:
            mt = p.stat().st_mtime
        except OSError:
            self._bundle = None
            return None
        if mt != self._mtime:
            self._mtime = mt
            try:
                self._bundle = kf.load_bundle(p)
                log.info("kids firmware %s (code %d) ready for Bluetooth updates", self._bundle.label,
                         self._bundle.code)
            except kf.BundleError as exc:
                log.warning("kids firmware %s: %s", p, exc)
                self._bundle = None
        return self._bundle

    def family_ok(self, b: kf.Bundle) -> bool:
        pk = getattr(self.node, "pubkey", "") if self.node else ""
        if not pk:
            return True
        return kf.family_tag(bytes.fromhex(pk), b.pubkey) == b.family

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        warned = False
        lock = FileLock(self.fw_dir / "kids-ota.lock")
        while not self._stop.is_set():
            b = self.bundle()
            link_up = self.node is None or getattr(self.node, "connected", True)
            if b and link_up:
                if not self.family_ok(b):
                    if not warned:
                        log.warning("kids firmware bundle was built for another room (family tag): not offered")
                        warned = True
                elif lock.try_acquire():
                    try:
                        await scan_and_offer(b, self.policy, adapter=self.adapter)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("kids update round failed: %s", exc)
                    finally:
                        lock.release()
            try:
                await asyncio.wait_for(self._stop.wait(), self.scan_minutes * 60)
            except asyncio.TimeoutError:
                pass


# --- CLI --------------------------------------------------------------------------------------

def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _cfg(env_path: str | None):
    return load_env(env_path)


def fw_dir_for(args, cfg) -> Path:
    if getattr(args, "firmware_dir", None):
        return Path(args.firmware_dir)
    if cfg.kids.firmware_dir:
        return Path(cfg.kids.firmware_dir)
    return default_fw_dir(cfg.db_path)


def firmware_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="spark-bridge kids-firmware",
                                 description="Manage the SpArkKids firmware this bridge offers over Bluetooth.")
    ap.add_argument("--env", "-e", default=None, help="the .env file (default as for the bridge)")
    ap.add_argument("--firmware-dir", default="", help="default: BRIDGE_KIDS_FIRMWARE_DIR, else kids-firmware/ "
                                                       "next to the bridge database")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("install", help="check a bundle (kids-ota.zip) and make it the one offered")
    ins.add_argument("bundle")
    ins.add_argument("--force", action="store_true", help="also replace a bundle with a higher code")
    sub.add_parser("show", help="the installed bundle")
    args = ap.parse_args(argv)
    try:
        cfg = _cfg(args.env)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    d = fw_dir_for(args, cfg)
    try:
        if args.cmd == "install":
            b = kf.install_bundle(args.bundle, d, force=args.force)
            print(f"installed {b.label} (code {b.code}, {b.size} bytes, family {b.family.hex()}) in {d}")
        else:
            b = kf.load_bundle(d / kf.CURRENT)
            print(f"{b.version}\ncode {b.code}  size {b.size}  family {b.family.hex()}  sha256 {b.sha256.hex()}")
    except kf.BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def update_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="spark-bridge kids-update",
        description="Scan for SpArkKids units of this family with an older version and offer them the "
                    "installed bundle (the unit asks: Install / Later). Runs alongside the bridge: it uses "
                    "its own Bluetooth connection and never the node link.")
    ap.add_argument("--env", "-e", default=None, help="the .env file (default as for the bridge)")
    ap.add_argument("--firmware-dir", default="", help="where the installed bundle is (see kids-firmware)")
    ap.add_argument("--bundle", default="", help="offer this bundle file instead of the installed one")
    ap.add_argument("--once", action="store_true", help="one scan round, then exit")
    ap.add_argument("--address", default="", help="only this unit (BLE address / macOS UUID)")
    ap.add_argument("--scan-seconds", type=float, default=20.0, help="scan time (macOS scans slowly while it holds other links)")
    ap.add_argument("--interval-minutes", type=float, default=5.0, help="without --once: time between rounds")
    ap.add_argument("--prompt-wait", type=float, default=150.0, help="how long to wait for a tap on the unit")
    ap.add_argument("--list", action="store_true", help="only list the kids units seen, offer nothing")
    ap.add_argument("--ble-adapter", default=None, metavar="hciN|MAC", help=ADAPTER_HELP)
    ap.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = _cfg(args.env)
        adapter = check_adapter(args.ble_adapter) if args.ble_adapter is not None else cfg.node.ble_adapter
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        b = kf.load_bundle(args.bundle or fw_dir_for(args, cfg) / kf.CURRENT)
    except kf.BundleError as exc:
        if not args.list:
            print(f"error: {exc} (install one with: spark-bridge kids-firmware install <kids-ota.zip>)",
                  file=sys.stderr)
            return 1
        b = None
    try:
        return asyncio.run(_update(args, b, adapter, fw_dir_for(args, cfg)))
    except AdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


async def _update(args, b: kf.Bundle | None, adapter: str, fw_dir: Path) -> int:
    if args.list:
        for u in await scan_units(args.scan_seconds, adapter):
            fam = "" if b is None else (" (this family)" if u.adv.family == b.family else " (other family)")
            flags = ",".join(n for n, on in (("snoozed", u.adv.snoozed), ("low-battery", u.adv.low_battery),
                                             ("busy", u.adv.busy)) if on)
            print(f"{u.address}  code {u.adv.code}  family {u.adv.family.hex()}{fam}  rssi {u.rssi}  {flags}")
        return 0
    lock = FileLock(fw_dir / "kids-ota.lock")
    if not lock.try_acquire():
        print("another kids update runs (the bridge, or a second kids-update): try again later", file=sys.stderr)
        return 1
    try:
        say(f"offering {b.label} (code {b.code}, family {b.family.hex()})")
        policy = kf.OfferPolicy()
        while True:
            res = await scan_and_offer(b, policy, adapter=adapter, scan_seconds=args.scan_seconds,
                                       address=args.address, say=say, prompt_wait=args.prompt_wait)
            if args.once:
                if not res:
                    say("no family unit needing this update in range")
                return 0 if all(o != "error" for _, o in res) else 1
            await asyncio.sleep(args.interval_minutes * 60)
    finally:
        lock.release()
