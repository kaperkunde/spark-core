"""dfu.py against a fake Adafruit bootloader (legacy DFU state machine, SDK 11 behaviour)."""
import asyncio
import io
import json
import struct
import zipfile

import pytest

from spark_bridge import dfu
from spark_bridge.dfu import (DfuError, PackageError, Timeouts, check_package, crc16, is_bootloader_adv,
                              load_package, mac_plus_one, parse_dfu_reply, transfer)

FAST = Timeouts(response=0.5, start=0.5, receipt=0.2, final=0.5, validate=0.5)
TAG = b"SPARK-BUILD v1.18.0-SparkNode1.1 ab12cd34-261122190000\x00"


def image(size=4000):
    body = struct.pack("<II", 0x20040000, 0x27000 + 0x200 + 1) + TAG
    body += bytes((i * 31) & 0xFF for i in range(size - len(body)))
    return body


def init_packet(img, dev_type=0x52, sds=(0x0123,)):
    return struct.pack("<HHIH", dev_type, 0xFFFF, 0xFFFFFFFF, len(sds)) + struct.pack(f"<{len(sds)}H", *sds) \
        + struct.pack("<H", crc16(img))


def zip_bytes(img, dat, manifest=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("firmware.bin", img)
        z.writestr("firmware.dat", dat)
        z.writestr("manifest.json", json.dumps(manifest or {"manifest": {"application": {
            "bin_file": "firmware.bin", "dat_file": "firmware.dat"}, "dfu_version": 0.5}}))
    return buf.getvalue()


# --- package ----------------------------------------------------------------------------------------

def test_crc16_matches_sdk():
    assert crc16(b"123456789") == 0x29B1


def test_package_ok(tmp_path):
    img = image()
    p = tmp_path / "fw.zip"
    p.write_bytes(zip_bytes(img, init_packet(img)))
    pkg = load_package(str(p))
    assert pkg.version == "v1.18.0-SparkNode1.1" and pkg.build_id == "ab12cd34-261122190000"
    assert pkg.build == "v1.18.0-SparkNode1.1 ab12cd34-261122190000"
    assert not pkg.warnings


@pytest.mark.parametrize("mutate,msg", [
    (lambda img, dat: (img, dat[:-2] + b"\x00\x00"), "CRC16"),
    (lambda img, dat: (img + b"\x01\x02", init_packet(img + b"\x01\x02")), "multiple of 4"),
    (lambda img, dat: (img, init_packet(img, dev_type=0x51)), "device type"),
    (lambda img, dat: (img, init_packet(img, sds=(0x00B6,))), "SoftDevice"),
    (lambda img, dat: (b"\xff" * 8 + img[8:], init_packet(b"\xff" * 8 + img[8:])), "stack pointer"),
])
def test_package_rejects(mutate, msg):
    img = image()
    img2, dat2 = mutate(img, init_packet(img))
    with pytest.raises(PackageError, match=msg):
        check_package("x", img2, dat2)


def test_package_oversize():
    img = image(4000)
    with pytest.raises(PackageError, match="post store"):
        check_package("x", img, init_packet(img), max_size=3996)


def test_package_not_application_only(tmp_path):
    img = image()
    p = tmp_path / "fw.zip"
    p.write_bytes(zip_bytes(img, init_packet(img), {"manifest": {"bootloader": {}, "application": {}}}))
    with pytest.raises(PackageError, match="only application"):
        load_package(str(p))


def test_package_without_tag_warns():
    img = bytearray(image())
    i = img.find(b"SPARK-BUILD")
    img[i:i + 5] = b"XXXXX"
    pkg = check_package("x", bytes(img), init_packet(bytes(img)))
    assert pkg.build_id == "" and pkg.warnings


def test_helpers():
    assert mac_plus_one("DA:98:76:54:32:10") == "DA:98:76:54:32:11"
    assert mac_plus_one("C0:00:00:00:00:FF") == "C0:00:00:00:00:00"
    assert parse_dfu_reply("OK - dfu da:98:76:54:32:11 in 2 s, battery 4000 mV, x") == "DA:98:76:54:32:11"
    with pytest.raises(DfuError, match="battery"):
        parse_dfu_reply("ERR - battery 3500 mV < 3700 mV")
    assert is_bootloader_adv([dfu.DFU_SERVICE.upper()], "AdaDFU")
    assert not is_bootloader_adv([dfu.NUS_SERVICE], "MeshCore-SparkNode")


# --- fake bootloader ----------------------------------------------------------------------------------

class FakeDevice:
    """The node: app or bootloader. Bootloader state survives reconnects within one session."""

    def __init__(self, *, spark=False, prn_skip_at=None, drop_packet=None, lose_link_at=None,
                 start_status=1, crc_fail=False, receipt_count_offset=0):
        self.spark = spark
        self.mode = "ota"  # ota | app | usb
        self.app_valid = True
        self.app = None
        self.sessions = 0
        self.resets = []  # ("to_app" | "to_ota" | "to_usb")
        self.writes = []  # control writes, all sessions
        self.prn_skip_at = prn_skip_at  # a receipt that never arrives (count still correct)
        self.drop_packet = drop_packet  # packet index lost in the air
        self.lose_link_at = lose_link_at  # bytes after which the link drops (once)
        self.start_status = start_status
        self.crc_fail = crc_fail
        self.receipt_count_offset = receipt_count_offset
        self.link_payload = 20  # ATT MTU - 3 the fake link reports
        self.packet_sizes = []  # data packet sizes seen, per session
        self._new_session()

    def _new_session(self):
        self.sessions += 1
        self.state = "idle"
        self.size = 0
        self.data = bytearray()
        self.pkt_mode = None
        self.prn = 0
        self.prn_cnt = 0
        self.npackets = 0
        self.init = b""
        self.validated = False

    def reset(self):
        """OP_RESET: what the bootloader does next."""
        incomplete = self.state == "rx" and len(self.data) != self.size
        if self.spark and (incomplete or not self.app_valid):
            self._new_session()
            self.resets.append("to_ota")
        elif self.app_valid:
            self.mode = "app"
            self.resets.append("to_app")
        else:
            self.mode = "usb"
            self.resets.append("to_usb")


class FakeTransport:
    def __init__(self, dev: FakeDevice):
        self.dev = dev
        self.max_packet_size = getattr(dev, "link_payload", 20)
        self.cb = None
        self.connected = False

    async def connect(self):
        if self.dev.mode != "ota":
            raise DfuError(f"no bootloader advertising (node is in {self.dev.mode} mode)")
        self.connected = True

    async def disconnect(self):
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    async def read_revision(self):
        return "0.11.0-spark1 s140 7.3.0" if self.dev.spark else "0.9.2 s140 7.3.0"

    async def start_notify(self, cb):
        self.cb = cb

    def _notify(self, data):
        asyncio.get_running_loop().call_soon(self.cb, bytes(data))

    def _resp(self, op, status=1, extra=b""):
        self._notify(bytes([0x10, op, status]) + extra)

    async def write_control(self, data):
        d = self.dev
        if not self.connected:
            raise OSError("not connected")
        d.writes.append(bytes(data))
        op = data[0]
        if op == 1:
            d.pkt_mode = "start"
        elif op == 2:
            if data[1] == 0:
                d.pkt_mode = "init"
            else:
                ok = d.state == "started" and len(d.init) == 14
                if ok:
                    d.state = "init"
                self._resp(2, 1 if ok else 2)
        elif op == 8:
            d.prn = data[1] | (data[2] << 8)
            d.prn_cnt = d.prn
        elif op == 3:
            if d.state == "init":
                d.state = "rx"
                d.pkt_mode = "data"
            else:
                self._resp(3, 2)
        elif op == 4:
            ok = len(d.data) == d.size and crc16(bytes(d.data)) == struct.unpack_from("<H", d.init, 12)[0]
            ok = ok and not d.crc_fail
            d.validated = ok
            self._resp(4, 1 if ok else 5)
        elif op == 5:
            self.connected = False
            if d.validated:
                d.app_valid = True
                d.app = bytes(d.data)
                d.mode = "app"
        elif op == 6:
            self.connected = False
            d.reset()
        elif op == 7:
            self._resp(7, 1, struct.pack("<I", len(d.data)))

    async def write_packet(self, data):
        d = self.dev
        if not self.connected:
            raise OSError("not connected")
        if d.pkt_mode == "start":
            d.pkt_mode = None
            if d.state != "idle":
                self._resp(1, 2)
                return
            d.size = struct.unpack("<III", data)[2]
            if d.start_status == 1:
                d.state = "started"
                d.app_valid = False  # the bank is erased now
            self._resp(1, d.start_status)
        elif d.pkt_mode == "init":
            d.init += data
        elif d.pkt_mode == "data" and d.state == "rx":
            idx = d.npackets
            d.npackets += 1
            d.packet_sizes.append(len(data))
            if d.lose_link_at is not None and len(d.data) >= d.lose_link_at:
                d.lose_link_at = None
                self.connected = False  # link loss: the bootloader re-advertises, same session
                return
            if idx == d.drop_packet:
                d.drop_packet = None
                return
            d.data += data
            if len(d.data) >= d.size:
                self._resp(3, 1)  # final packet: response, no receipt
                return
            d.prn_cnt -= 1
            if d.prn and d.prn_cnt == 0:
                d.prn_cnt = d.prn
                if d.prn_skip_at is not None and len(d.data) >= d.prn_skip_at:
                    d.prn_skip_at = None
                    return
                self._notify(bytes([0x11]) + struct.pack("<I", len(d.data) + d.receipt_count_offset))


def pkg_of(size=4000):
    img = image(size)
    return check_package("t", img, init_packet(img))


async def run(dev, pkg, **kw):
    async def connect():
        t = FakeTransport(dev)
        await t.connect()
        return t

    async def nosleep(_s):
        pass

    return await transfer(connect, pkg, timeouts=FAST, sleep=nosleep, **kw)


async def test_happy_path_and_prn_boundary():
    # 4000 bytes = 200 packets = 25 x 8: the last packet closes a PRN window and must not wait
    pkg = pkg_of(4000)
    dev = FakeDevice()
    rev = await run(dev, pkg)
    assert dev.mode == "app" and dev.app == pkg.image and rev.startswith("0.9.2")
    assert bytes([6]) not in dev.writes  # never a Reset on success


async def test_odd_size_and_progress():
    pkg = pkg_of(4004)
    seen = []
    dev = FakeDevice()
    await run(dev, pkg, progress=lambda a, b: seen.append(a))
    assert dev.app == pkg.image and seen[-1] == 4004 and seen == sorted(seen)


async def test_dropped_packet_is_detected_by_receipt_count():
    dev = FakeDevice(drop_packet=13)
    # the receipt window shifts by one packet: the receipt is late, the size request shows 300 != 320
    with pytest.raises(DfuError, match="bootloader has 300 bytes, 320 sent") as ei:
        await run(dev, pkg_of(), attempts=1)
    assert ei.value.app_erased
    assert bytes([6]) not in dev.writes  # stock bootloader after Start: no Reset
    assert dev.mode == "ota"


async def test_receipt_timeout_checked_with_size_request():
    dev = FakeDevice(prn_skip_at=800)
    await run(dev, pkg_of())
    assert bytes([7]) in dev.writes and dev.mode == "app"


async def test_receipt_timeout_with_wrong_count_aborts():
    dev = FakeDevice(prn_skip_at=800, drop_packet=50)  # receipt at 800 missing, then a lost packet
    with pytest.raises(DfuError):
        await run(dev, pkg_of(), attempts=1)


async def test_wrong_receipt_count_aborts():
    dev = FakeDevice(receipt_count_offset=-20)
    with pytest.raises(DfuError, match="packet lost"):
        await run(dev, pkg_of(), attempts=1)


async def test_link_loss_after_start_stock_bootloader_no_reset_no_retry():
    dev = FakeDevice(lose_link_at=1000)
    with pytest.raises(DfuError, match="stock bootloader") as ei:
        await run(dev, pkg_of(), attempts=3)
    assert ei.value.app_erased
    assert bytes([6]) not in dev.writes
    assert dev.sessions == 1 and dev.mode == "ota"


async def test_link_loss_after_start_spark_bootloader_resets_and_retries():
    dev = FakeDevice(spark=True, lose_link_at=1000)
    await run(dev, pkg_of(), attempts=3)
    assert dev.resets == ["to_ota"] and dev.sessions == 2
    assert dev.mode == "app" and dev.app == pkg_of().image


async def test_spark_bootloader_gives_up_after_attempts_and_stays_in_ota():
    dev = FakeDevice(spark=True, crc_fail=True)
    with pytest.raises(DfuError, match="CRC") as ei:
        await run(dev, pkg_of(), attempts=2)
    assert ei.value.app_erased
    assert dev.mode == "ota" and dev.resets == ["to_ota", "to_ota"]


async def test_start_refused_resets_to_old_app():
    dev = FakeDevice(start_status=4)
    with pytest.raises(DfuError, match="data size") as ei:
        await run(dev, pkg_of(), attempts=2)
    assert not ei.value.app_erased
    assert dev.resets == ["to_app"] and dev.app_valid and dev.mode == "app"


async def test_start_timeout_before_start_retries_in_same_session():
    dev = FakeDevice()
    calls = {"n": 0}

    async def connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("scan found nothing")
        t = FakeTransport(dev)
        await t.connect()
        return t

    async def nosleep(_s):
        pass

    await transfer(connect, pkg_of(), timeouts=FAST, sleep=nosleep, attempts=2)
    assert dev.mode == "app" and calls["n"] == 2


async def test_dirty_session_spark_bootloader_resets_and_completes():
    # an earlier run broke off mid-transfer (tool killed): the session still expects data
    dev = FakeDevice(spark=True)
    dev.state, dev.size, dev.app_valid = "rx", 4000, False
    await run(dev, pkg_of(), attempts=2)
    assert dev.resets == ["to_ota"] and dev.mode == "app"


async def test_dirty_session_stock_bootloader_is_not_reset():
    dev = FakeDevice()
    dev.state, dev.size, dev.app_valid = "rx", 4000, False
    with pytest.raises(DfuError, match="broke off") as ei:
        await run(dev, pkg_of(), attempts=3)
    assert ei.value.app_erased and bytes([6]) not in dev.writes


async def test_large_packets_when_the_link_allows_and_prn_counts_packets():
    pkg = pkg_of(4004)
    dev = FakeDevice()
    dev.link_payload = 244
    await run(dev, pkg)
    assert dev.app == pkg.image
    assert set(dev.packet_sizes[:-1]) == {244} and dev.packet_sizes[-1] == 4004 % 244
    assert all(n % 4 == 0 for n in dev.packet_sizes)


def test_choose_packet_size():
    assert dfu.choose_packet_size(0, 247 - 3) == 244
    assert dfu.choose_packet_size(0, 23 - 3) == 20
    assert dfu.choose_packet_size(0, 0) == 20
    assert dfu.choose_packet_size(0, 512) == 244  # never above what the bootloader takes
    assert dfu.choose_packet_size(100, 244) == 100
    assert dfu.choose_packet_size(101, 244) == 100  # multiple of 4
    assert dfu.choose_packet_size(20, 244) == 20
    assert dfu.choose_packet_size(-5, 244) == 244 and dfu.choose_packet_size(7, 244) == 20


async def test_retry_after_failure_falls_back_to_legacy_packets():
    pkg = pkg_of(4000)
    dev = FakeDevice(spark=True, lose_link_at=1000)
    dev.link_payload = 244
    await run(dev, pkg)
    assert dev.app == pkg.image
    # first session: large packets until the link broke; the retry (fresh session): 20 bytes
    assert 244 in dev.packet_sizes and dev.packet_sizes[-1] == 20 and dev.packet_sizes.count(20) > 100


async def test_explicit_packet_size_and_prn_validation():
    pkg = pkg_of(4000)
    dev = FakeDevice()
    dev.link_payload = 244
    await run(dev, pkg, packet_size=20)
    assert set(dev.packet_sizes) == {20}
    for bad in (0, 9, 16):
        with pytest.raises(ValueError):
            dfu.DfuSession(FakeTransport(FakeDevice()), pkg, prn=bad)
    await run(FakeDevice(), pkg, prn=1)
