"""BLE OTA update of a SparkNode through the Adafruit nRF52 bootloader (Nordic legacy DFU, SDK 11).

Application images only. Modelled on nrf-ota's dfu.py (OpenDisplay, Apache-2.0) and the
bootloader's dfu_transport_ble.c, with a stricter failure policy:

- every packet receipt notification (PRN, every 8 packets of 20 bytes) is awaited and its byte
  count must equal the bytes sent; on a receipt timeout the bootloader is asked for its count
  (opcode 0x07) and the transfer continues only if it matches, otherwise it is aborted;
- the final packet gets no receipt (the bootloader sends the Receive response instead), so its
  receipt is not awaited even when it closes a PRN window;
- Start may take long (the bootloader erases the old app first): up to 60 s;
- before Start is accepted, a failure sends Reset: the old app is intact and boots again;
- after Start the old app is gone. Reset is then sent only to a Spark bootloader (DIS firmware
  revision contains "spark"), which comes back in OTA mode for a retry. A stock bootloader
  would come back in USB mode, so it never gets a Reset after Start;
- after Activate, success is not claimed here: the caller verifies the running build.

The transport is an interface so the protocol logic is tested against a fake bootloader
(tests/test_dfu.py) without hardware.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

log = logging.getLogger("spark_bridge.dfu")

DFU_SERVICE = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL = "00001531-1212-efde-1523-785feabcd123"
DFU_PACKET = "00001532-1212-efde-1523-785feabcd123"
DIS_FW_REV = "00002a26-0000-1000-8000-00805f9b34fb"
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

OP_START, OP_INIT, OP_RECEIVE, OP_VALIDATE, OP_ACTIVATE, OP_RESET, OP_SIZE, OP_PRN_REQ = 1, 2, 3, 4, 5, 6, 7, 8
OP_RESPONSE, OP_RECEIPT = 0x10, 0x11
IMAGE_APPLICATION = 0x04
STATUS = {1: "success", 2: "invalid state", 3: "not supported", 4: "data size exceeds limit", 5: "CRC error",
          6: "operation failed"}

PACKET_SIZE = 20
PRN = 8  # the bootloader README: 8 or less, or it runs out of buffers

# SparkNode limits (variants/spark_p1/platformio.ini): app region 0x27000..0xD4000; above it the
# erase would reach the room post store.
APP_START = 0x27000
APP_MAX_SIZE = 708608
DEVICE_TYPE = 0x0052
SOFTDEVICE_S140_730 = 0x0123
BUILD_TAG = re.compile(rb"SPARK-BUILD (v[0-9A-Za-z.+-]+) ([0-9A-Za-z.+-]+)\x00")


class DfuError(Exception):
    """A failed update. `app_erased` tells whether the node is left without an application."""

    def __init__(self, msg: str, *, app_erased: bool = False):
        super().__init__(msg)
        self.app_erased = app_erased


class PackageError(Exception):
    pass


# --- package ------------------------------------------------------------------------------------

def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """Nordic SDK crc16_compute (CRC-16/CCITT-FALSE)."""
    for b in data:
        crc = ((crc >> 8) & 0xFF) | ((crc << 8) & 0xFFFF)
        crc ^= b
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc


@dataclass
class InitPacket:
    device_type: int
    device_rev: int
    app_version: int
    softdevices: list[int]
    crc: int

    @classmethod
    def parse(cls, dat: bytes) -> "InitPacket":
        if len(dat) < 12:
            raise PackageError(f"init packet too short ({len(dat)} bytes)")
        dev_type, dev_rev, app_ver, n_sd = struct.unpack_from("<HHIH", dat, 0)
        need = 10 + 2 * n_sd + 2
        if len(dat) != need:
            raise PackageError(f"init packet is {len(dat)} bytes, expected {need} (legacy DFU, CRC16)")
        sds = list(struct.unpack_from(f"<{n_sd}H", dat, 10))
        (crc,) = struct.unpack_from("<H", dat, 10 + 2 * n_sd)
        return cls(dev_type, dev_rev, app_ver, sds, crc)


@dataclass
class Package:
    path: str
    image: bytes
    init: bytes
    init_info: InitPacket
    version: str = ""  # "v1.18.0-SparkNode2", from the image's SPARK-BUILD tag
    build_id: str = ""  # the node's `get build` must report this after the update
    warnings: list[str] = field(default_factory=list)

    @property
    def build(self) -> str:
        return f"{self.version} {self.build_id}".strip()


def load_package(path: str, *, max_size: int = APP_MAX_SIZE) -> Package:
    """Read and check an adafruit-nrfutil application zip (offline preflight)."""
    try:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))["manifest"]
            if set(manifest) - {"application", "dfu_version"}:
                raise PackageError(f"only application packages are supported, manifest has {sorted(manifest)}")
            app = manifest.get("application")
            if not app:
                raise PackageError("manifest has no application")
            image = z.read(app["bin_file"])
            init = z.read(app["dat_file"])
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise PackageError(f"{path}: not a DFU package ({exc})") from exc
    return check_package(path, image, init, max_size=max_size)


def check_package(path: str, image: bytes, init: bytes, *, max_size: int = APP_MAX_SIZE) -> Package:
    info = InitPacket.parse(init)
    if info.device_type != DEVICE_TYPE:
        raise PackageError(f"device type 0x{info.device_type:04x}, expected 0x{DEVICE_TYPE:04x}")
    if SOFTDEVICE_S140_730 not in info.softdevices and 0xFFFE not in info.softdevices:
        raise PackageError(f"SoftDevice requirement {[hex(s) for s in info.softdevices]} does not allow S140 7.3.0 (0x0123)")
    if not image or len(image) % 4:
        raise PackageError(f"image length {len(image)} is not a positive multiple of 4")
    if len(image) > max_size:
        raise PackageError(f"image is {len(image)} bytes, more than {max_size}: it would overwrite the post store")
    if crc16(image) != info.crc:
        raise PackageError(f"image CRC16 0x{crc16(image):04x} does not match the init packet (0x{info.crc:04x})")
    sp, reset = struct.unpack_from("<II", image, 0)
    if not (0x20000000 < sp <= 0x20040000):
        raise PackageError(f"vector table: initial stack pointer 0x{sp:08x} is not in RAM")
    if not (reset & 1) or not (APP_START <= (reset & ~1) < APP_START + len(image)):
        raise PackageError(f"vector table: reset handler 0x{reset:08x} is not in the image")
    pkg = Package(path, image, init, info)
    m = BUILD_TAG.search(image)
    if m:
        pkg.version, pkg.build_id = m.group(1).decode(), m.group(2).decode()
    else:
        pkg.warnings.append("no SPARK-BUILD tag in the image: not a SparkNode2+ build, the result cannot be verified")
    return pkg


# --- transport ------------------------------------------------------------------------------------

class Transport(Protocol):
    """One connection to the bootloader's DFU service."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
    async def read_revision(self) -> str: ...  # DIS firmware revision, "" if absent
    async def start_notify(self, callback: Callable[[bytes], None]) -> None: ...
    async def write_control(self, data: bytes) -> None: ...  # write with response
    async def write_packet(self, data: bytes) -> None: ...  # write without response


class BleakTransport:
    """Transport over bleak, for a BLEDevice or address found by the caller."""

    def __init__(self, device, *, timeout: float = 20.0):
        self.device = device
        self.timeout = timeout
        self.client = None
        self._lost = asyncio.Event()

    async def connect(self) -> None:
        from bleak import BleakClient

        self._lost.clear()
        self.client = BleakClient(self.device, timeout=self.timeout,
                                  disconnected_callback=lambda _c: self._lost.set())
        await self.client.connect()
        if self.client.services.get_characteristic(DFU_CONTROL) is None:
            await self.disconnect()
            raise DfuError("connected device has no DFU control point (not the bootloader?)")

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await asyncio.wait_for(self.client.disconnect(), 10)
            except Exception as exc:  # noqa: BLE001
                log.debug("disconnect: %s", exc)

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected and not self._lost.is_set()

    async def read_revision(self) -> str:
        try:
            return bytes(await self.client.read_gatt_char(DIS_FW_REV)).decode(errors="replace").strip("\x00 ")
        except Exception as exc:  # noqa: BLE001
            log.debug("no DIS firmware revision: %s", exc)
            return ""

    async def start_notify(self, callback) -> None:
        await self.client.start_notify(DFU_CONTROL, lambda _c, data: callback(bytes(data)))

    async def write_control(self, data: bytes) -> None:
        await self.client.write_gatt_char(DFU_CONTROL, data, response=True)

    async def write_packet(self, data: bytes) -> None:
        await self.client.write_gatt_char(DFU_PACKET, data, response=False)


# --- protocol -------------------------------------------------------------------------------------

@dataclass
class Timeouts:
    response: float = 10.0
    start: float = 60.0  # Start includes erasing the old application
    receipt: float = 5.0
    final: float = 20.0  # last flash write, then the Receive response
    validate: float = 20.0


class DfuSession:
    """One attempt on one connection: Start .. Activate. Raises DfuError on any failure."""

    def __init__(self, transport: Transport, pkg: Package, *, prn: int = PRN, timeouts: Timeouts | None = None,
                 progress: Callable[[int, int], None] | None = None):
        self.t = transport
        self.pkg = pkg
        self.prn = prn
        self.to = timeouts or Timeouts()
        self.progress = progress
        self.started = False  # Start accepted: from here on the old app is erased
        self.spark_bootloader = False
        self.revision = ""
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_notify(self, data: bytes) -> None:
        self._q.put_nowait(data)

    async def _next(self, timeout: float) -> bytes:
        return await asyncio.wait_for(self._q.get(), timeout)

    async def _response(self, op: int, timeout: float, what: str) -> bytes:
        """Wait for the response to `op`; receipts in between are ignored."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            left = end - loop.time()
            if left <= 0 or not self.t.is_connected:
                raise DfuError(f"{what}: no response ({'link lost' if not self.t.is_connected else 'timeout'})",
                               app_erased=self.started)
            try:
                msg = await self._next(min(left, 1.0))
            except asyncio.TimeoutError:
                continue
            if len(msg) >= 3 and msg[0] == OP_RESPONSE and msg[1] == op:
                if msg[2] != 1:
                    raise DfuError(f"{what}: {STATUS.get(msg[2], msg[2])}", app_erased=self.started)
                return msg
            if msg[:1] != bytes([OP_RECEIPT]):
                log.debug("unexpected notification %s while waiting for %s", msg.hex(), what)

    async def bootloader_count(self) -> int:
        await self.t.write_control(bytes([OP_SIZE]))
        msg = await self._response(OP_SIZE, self.to.response, "image size report")
        if len(msg) < 7:
            raise DfuError("image size report too short", app_erased=self.started)
        return struct.unpack_from("<I", msg, 3)[0]

    async def run(self) -> None:
        img = self.pkg.image
        await self.t.start_notify(self._on_notify)
        self.revision = await self.t.read_revision()
        self.spark_bootloader = "spark" in self.revision.lower()
        log.info("bootloader %s", self.revision or "(no revision)")

        # Start: from the accepted response on, the old application is erased
        await self.t.write_control(bytes([OP_START, IMAGE_APPLICATION]))
        await self.t.write_packet(struct.pack("<III", 0, 0, len(img)))
        try:
            await self._response(OP_START, self.to.start, "Start")
        except DfuError as exc:
            if "invalid state" in str(exc):
                # this bootloader session already accepted a Start (an earlier transfer broke
                # off): the old app is gone and only a new session can take a new Start
                self.started = True
                raise DfuError("Start refused: an earlier transfer in this bootloader session broke off",
                               app_erased=True) from None
            raise
        self.started = True

        await self.t.write_control(bytes([OP_INIT, 0]))
        await self.t.write_packet(self.pkg.init)
        await self.t.write_control(bytes([OP_INIT, 1]))
        await self._response(OP_INIT, self.to.response, "Init packet")

        await self.t.write_control(struct.pack("<BH", OP_PRN_REQ, self.prn))
        await self.t.write_control(bytes([OP_RECEIVE]))

        packets = [img[i:i + PACKET_SIZE] for i in range(0, len(img), PACKET_SIZE)]
        sent = 0
        for n, chunk in enumerate(packets, 1):
            if not self.t.is_connected:
                raise DfuError(f"link lost after {sent} of {len(img)} bytes", app_erased=True)
            await self.t.write_packet(chunk)
            sent += len(chunk)
            if n == len(packets):
                break  # the final packet gets the Receive response, not a receipt
            if n % self.prn == 0:
                await self._receipt(sent)
                if self.progress:
                    self.progress(sent, len(img))
        await self._response(OP_RECEIVE, self.to.final, "Receive")
        if self.progress:
            self.progress(len(img), len(img))

        await self.t.write_control(bytes([OP_VALIDATE]))
        await self._response(OP_VALIDATE, self.to.validate, "Validate")
        try:
            await self.t.write_control(bytes([OP_ACTIVATE]))
        except Exception as exc:  # noqa: BLE001  the bootloader may drop the link while answering
            log.debug("activate write: %s", exc)

    async def _receipt(self, sent: int) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.to.receipt
        while True:
            left = end - loop.time()
            if not self.t.is_connected:
                raise DfuError(f"link lost after {sent} bytes", app_erased=True)
            if left <= 0:
                got = await self.bootloader_count()  # PRN timeout: ask, never assume
                if got != sent:
                    raise DfuError(f"receipt timeout: bootloader has {got} bytes, {sent} sent", app_erased=True)
                log.warning("receipt timeout at %d bytes; the bootloader confirms the count", sent)
                return
            try:
                msg = await self._next(min(left, 0.5))
            except asyncio.TimeoutError:
                continue
            if len(msg) >= 5 and msg[0] == OP_RECEIPT:
                (got,) = struct.unpack_from("<I", msg, 1)
                if got != sent:
                    raise DfuError(f"receipt says {got} bytes, {sent} sent: packet lost", app_erased=True)
                return
            if len(msg) >= 3 and msg[0] == OP_RESPONSE:
                raise DfuError(f"bootloader stopped the transfer: {STATUS.get(msg[2], msg[2])}", app_erased=True)


async def transfer(connect: Callable[[], Awaitable[Transport]], pkg: Package, *, attempts: int = 3,
                   prn: int = PRN, timeouts: Timeouts | None = None, progress=None,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, reboot_wait: float = 4.0) -> str:
    """Run DfuSession with the retry policy and return the bootloader revision.

    `connect` returns a Transport connected to the bootloader (it scans by advertisement).
    - Failure before Start: retry Start in the same, still idle, bootloader session; after the
      last attempt send Reset so the old application boots again.
    - Failure after Start: a Spark bootloader gets Reset (it restarts in OTA mode, the app is
      already gone) and the next attempt starts over; a stock bootloader is left alone.
    Raises DfuError (with app_erased) when it gives up.
    """
    last: DfuError | None = None
    erased = False
    spark = False
    for attempt in range(1, attempts + 1):
        t: Transport | None = None
        s: DfuSession | None = None
        failed = True
        try:
            t = await connect()
            s = DfuSession(t, pkg, prn=prn, timeouts=timeouts, progress=progress)
            await s.run()
            failed = False
            return s.revision
        except Exception as exc:  # noqa: BLE001
            if s is not None:
                erased = erased or s.started
                spark = spark or s.spark_bootloader
            last = exc if isinstance(exc, DfuError) else DfuError(f"{type(exc).__name__}: {exc}")
            last.app_erased = erased
            log.warning("attempt %d/%d failed: %s", attempt, attempts, last)
        finally:
            if failed and t is not None and not erased and attempt == attempts:
                await _reset(t, connect)  # give up before Start: the old app boots again
            elif failed and t is not None and erased and spark:
                await _reset(t, connect)  # Spark bootloader: restart the OTA session
            if t is not None:
                await t.disconnect()
        if erased and not spark:
            raise DfuError(f"{last} (stock bootloader: no retry after Start; the node waits in this "
                           "bootloader session, see spark/README.md 'Recovery')", app_erased=True)
        if erased:
            await sleep(reboot_wait)
    assert last is not None
    raise last


async def _reset(t: Transport, connect: Callable[[], Awaitable[Transport]]) -> None:
    """Send Reset, reconnecting once if the link is gone."""
    try:
        if not t.is_connected:
            await t.disconnect()
            t2 = await connect()
            try:
                await t2.write_control(bytes([OP_RESET]))
            finally:
                await t2.disconnect()
            return
        await t.write_control(bytes([OP_RESET]))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not send Reset to the bootloader: %s", exc)


# --- discovery helpers ------------------------------------------------------------------------------

def mac_plus_one(mac: str) -> str:
    b = mac.split(":")
    if len(b) != 6:
        raise ValueError(mac)
    return ":".join(b[:5] + [f"{(int(b[5], 16) + 1) & 0xFF:02X}"])


REPLY_MAC = re.compile(r"OK - dfu ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})")


def parse_dfu_reply(reply: str) -> str:
    m = REPLY_MAC.search(reply)
    if not m:
        raise DfuError(f"node refused dfu: {reply.strip() or '(empty reply)'}")
    return m.group(1).upper()


def is_bootloader_adv(service_uuids, name: str | None) -> bool:
    uuids = [u.lower() for u in (service_uuids or [])]
    return DFU_SERVICE in uuids and NUS_SERVICE not in uuids
