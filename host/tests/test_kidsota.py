"""Kids OTA: bundles, signatures, wire formats, back-off policy and the transfer session."""

import asyncio
import hashlib
import io
import json
import struct
import zipfile

import pytest

from spark_bridge import ed25519, kidsfw as kf
from spark_bridge.config import from_env
from spark_bridge.kidsota import FileLock, Session, Unit, pick

SEED = bytes(range(32))
ROOM = bytes([0xA5] * 32)


def image(n=5000, fill=0x11):
    return bytes([0xE9]) + bytes([fill]) * (n - 1)


def bundle_bytes(code=102, img=None, seed=SEED, room=ROOM):
    return kf.make_bundle(img or image(), code, f"SpArkKids-test{code} (MeshCore v1.18.0)", seed, room)


# --- ed25519 ------------------------------------------------------------------------------------

def test_ed25519_rfc8032_vectors():
    sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pk = ed25519.public_key(sk)
    assert pk.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = ed25519.sign(sk, b"")
    assert sig.hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
                         "1cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert ed25519.verify(pk, b"", sig)
    sk2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    sig2 = ed25519.sign(sk2, bytes([0x72]))
    assert sig2.hex().startswith("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da")
    assert ed25519.verify(ed25519.public_key(sk2), bytes([0x72]), sig2)


def test_ed25519_rejects_tampering():
    pk = ed25519.public_key(SEED)
    sig = ed25519.sign(SEED, b"hello")
    assert not ed25519.verify(pk, b"hellp", sig)
    assert not ed25519.verify(pk, b"hello", sig[:63] + bytes([sig[63] ^ 1]))
    assert not ed25519.verify(ed25519.public_key(bytes(32)), b"hello", sig)
    assert not ed25519.verify(pk, b"hello", sig[:32] + (2**255 - 1).to_bytes(32, "little"))  # s >= L


# Cross-implementation vector: the same values are checked by MeshCore's orlp/ed25519 on the
# device side in test/test_spark_kids_ota (keep both in sync).
VECTOR_SEED = bytes(range(32))
VECTOR_SHA = hashlib.sha256(b"spark kids ota test image").digest()


def test_cross_vector_matches_native_test():
    msg = kf.signed_message(102, 123456, VECTOR_SHA)
    assert msg[:16] == b"SPARK-KIDS-OTA-1" and len(msg) == 56
    pub = ed25519.public_key(VECTOR_SEED)
    sig = ed25519.sign(VECTOR_SEED, msg)
    assert pub.hex() == "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
    assert sig.hex() == VECTOR_SIG


VECTOR_SIG = ("7aaa779b981bbd9478ca705e6916e23d1fa3ba627c5eb6a5251de1d5c4273942"
              "9ea9f3dcac7954d32d25c52990d25c8384e2e2d8dc86332eb563a3bc3e6ed703")


# --- bundles ------------------------------------------------------------------------------------

def test_bundle_roundtrip_and_offer():
    b = kf.parse_bundle(bundle_bytes())
    assert b.code == 102 and b.size == 5000 and b.label == "SpArkKids-test102"
    assert b.family == kf.family_tag(ROOM, ed25519.public_key(SEED))
    off = b.offer()
    assert off[0] == kf.CMD_OFFER and len(off) == 1 + 8 + 32 + 64 + len(b.label)
    code, size = struct.unpack_from("<II", off, 1)
    assert (code, size) == (102, 5000) and off[9:41] == b.sha256 and off[41:105] == b.sig


def _rezip(data: bytes, manifest_edit=None, image_edit=None) -> bytes:
    z = zipfile.ZipFile(io.BytesIO(data))
    m, img = json.loads(z.read("manifest.json")), z.read("firmware.bin")
    if manifest_edit:
        manifest_edit(m)
    if image_edit:
        img = image_edit(img)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as w:
        w.writestr("manifest.json", json.dumps(m))
        w.writestr("firmware.bin", img)
    return out.getvalue()


@pytest.mark.parametrize("edit,err", [
    (lambda m: m.update(code=103), "bad signature"),            # a higher code needs a new signature
    (lambda m: m.update(board="other"), "board"),
    (lambda m: m.update(component="x"), "not a SpArkKids bundle"),
    (lambda m: m.update(sig="00" * 64), "bad signature"),
    (lambda m: m.update(sha256="zz"), "not hex"),
])
def test_bundle_rejects_bad_manifest(edit, err):
    with pytest.raises(kf.BundleError, match=err):
        kf.parse_bundle(_rezip(bundle_bytes(), manifest_edit=edit))


def test_bundle_rejects_changed_image_and_garbage():
    with pytest.raises(kf.BundleError, match="sha256"):
        kf.parse_bundle(_rezip(bundle_bytes(), image_edit=lambda i: i[:-1] + b"\x00"))
    with pytest.raises(kf.BundleError, match="not a SpArkKids bundle"):
        kf.parse_bundle(b"not a zip")


def test_install_bundle_keeps_the_highest(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    a.write_bytes(bundle_bytes(103))
    b.write_bytes(bundle_bytes(102))
    d = tmp_path / "fw"
    assert kf.install_bundle(a, d).code == 103
    with pytest.raises(kf.BundleError, match="higher code"):
        kf.install_bundle(b, d)
    assert kf.install_bundle(b, d, force=True).code == 102
    assert kf.load_bundle(d / kf.CURRENT).code == 102


# --- wire formats -------------------------------------------------------------------------------

def test_advert_roundtrip_and_foreign_data():
    fam = bytes.fromhex("01020304")
    p = kf.encode_advert(fam, 101, kf.ADV_SNOOZED)
    assert len(p) == 12
    a = kf.parse_advert({kf.COMPANY_ID: p})
    assert a.family == fam and a.code == 101 and a.snoozed and not a.low_battery
    assert kf.parse_advert({0x004C: p}) is None             # someone else's company id
    assert kf.parse_advert({kf.COMPANY_ID: b"XX" + p[2:]}) is None
    assert kf.parse_advert({}) is None


def test_status_and_events():
    raw = struct.pack("<BIBIH", 1, 101, kf.ADV_SNOOZED, 3600, 3900) + b"SpArkKids1.1"
    st = kf.parse_status(raw)
    assert (st.code, st.snooze_secs, st.batt_mv, st.label) == (101, 3600, 3900, "SpArkKids1.1")
    ev = kf.parse_event(bytes([kf.NTF_STATE, kf.ST_REFUSED, kf.R_LOW_BATTERY]) + struct.pack("<I", 3500))
    assert ev.state == kf.ST_REFUSED and ev.reason == kf.R_LOW_BATTERY and "low battery" in ev.describe()
    assert kf.parse_event(bytes([kf.NTF_ACK]) + struct.pack("<I", 4096)).value == 4096
    assert kf.parse_event(b"\x81") is None


def test_short_label():
    assert kf.short_label("SpArkKids1.1 (MeshCore v1.18.0)") == "SpArkKids1.1"
    assert len(kf.short_label("x" * 40)) == kf.LABEL_MAX


# --- policy -------------------------------------------------------------------------------------

def adv(code=101, flags=0, fam=None, b=None):
    return kf.Advert(family=fam if fam is not None else b.family, code=code, flags=flags)


def test_policy_filters():
    b = kf.parse_bundle(bundle_bytes(102))
    p = kf.OfferPolicy()
    assert p.should_offer("u", adv(101, b=b), b, 0)[0]
    assert p.should_offer("u", adv(102, b=b), b, 0) == (False, "up to date")
    assert p.should_offer("u", adv(103, b=b), b, 0)[1] == "up to date"
    assert p.should_offer("u", adv(101, fam=b"\0\0\0\0"), b, 0)[1] == "other family"
    assert not p.should_offer("u", adv(101, kf.ADV_SNOOZED, b=b), b, 0)[0]
    assert not p.should_offer("u", adv(101, kf.ADV_LOW_BATTERY, b=b), b, 0)[0]
    assert not p.should_offer("u", adv(101, kf.ADV_BUSY, b=b), b, 0)[0]


def test_policy_backoff_snooze_and_errors():
    b = kf.parse_bundle(bundle_bytes(102))
    p = kf.OfferPolicy()
    a = adv(101, b=b)
    # the unit says snoozed with 2 h left: wait exactly that
    assert p.record("u", "snoozed", b, 1000, 7200) == 1000 + 7200
    assert not p.should_offer("u", a, b, 1000 + 7199)[0]
    assert p.should_offer("u", a, b, 1000 + 7200)[0]
    # a unit can never make us wait longer than 12 h, nor reconnect within a minute
    assert p.record("u", "snoozed", b, 0, 10 ** 9) == kf.SNOOZE_SECS
    assert p.record("u", "snoozed", b, 0, 1) == 60
    assert p.record("u", "noanswer", b, 0) == 3600
    # connection errors back off exponentially up to 30 min
    waits = [p.record("v", "error", b, 0) for _ in range(8)]
    assert waits[:3] == [60, 120, 240] and waits[-1] == 1800
    # a new bundle (code) starts fresh for that unit
    b2 = kf.parse_bundle(bundle_bytes(103))
    assert p.should_offer("u", a, b2, 1)[0]


def test_outcome_mapping():
    E = kf.Event
    assert kf.outcome_for(E(kf.NTF_STATE, kf.ST_SNOOZED, 0, 500)) == ("snoozed", 500.0)
    assert kf.outcome_for(E(kf.NTF_STATE, kf.ST_NOANSWER))[0] == "noanswer"
    assert kf.outcome_for(E(kf.NTF_STATE, kf.ST_REFUSED, kf.R_LOW_BATTERY))[0] == "low_battery"
    assert kf.outcome_for(E(kf.NTF_STATE, kf.ST_REFUSED, kf.R_BAD_SIGNATURE))[0] == "refused"
    assert kf.outcome_for(E(kf.NTF_STATE, kf.ST_DONE))[0] == "done"
    assert kf.outcome_for(E(kf.NTF_STATE, kf.ST_FAILED, kf.R_HASH_MISMATCH))[0] == "error"


def test_pick_orders_by_rssi_and_skips():
    b = kf.parse_bundle(bundle_bytes(102))
    units = [Unit(type("D", (), {"address": "far"})(), adv(101, b=b), -90),
             Unit(type("D", (), {"address": "near"})(), adv(101, b=b), -40),
             Unit(type("D", (), {"address": "new"})(), adv(102, b=b), -30)]
    assert [u.address for u in pick(units, b, kf.OfferPolicy(), 0)] == ["near", "far"]


def test_config_keys(tmp_path):
    cfg = from_env({"BRIDGE_KIDS_OTA": "0", "BRIDGE_KIDS_FIRMWARE_DIR": "/x", "BRIDGE_KIDS_SCAN_MINUTES": "7"})
    assert (cfg.kids.enabled, cfg.kids.firmware_dir, cfg.kids.scan_minutes) == (False, "/x", 7.0)
    assert from_env({}).kids.enabled


def test_file_lock(tmp_path):
    a, b = FileLock(tmp_path / "l"), FileLock(tmp_path / "l")
    assert a.try_acquire() and not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


# --- session against a simulated unit -----------------------------------------------------------

class FakeUnit:
    """Behaves like KidsOta.cpp: answers offers, buffers data in order, acks every 4 KB, NACKs gaps."""

    def __init__(self, decision="install", drop_every=0, my_code=101, pub=None):
        self.decision, self.drop_every, self.my_code = decision, drop_every, my_code
        self.pub = pub or ed25519.public_key(SEED)
        self.cb = None
        self.offer = None
        self.rx = bytearray()
        self.acked = 0
        self.writes = 0
        self.services = self

    def get_characteristic(self, _uuid):
        return type("C", (), {"max_write_without_response_size": 182})()

    async def read_gatt_char(self, _uuid):
        return struct.pack("<BIBIH", 1, self.my_code, 0, 0, 4000) + b"SpArkKids1.1"

    async def start_notify(self, _uuid, cb):
        self.cb = cb

    def notify(self, b):
        asyncio.get_running_loop().call_soon(self.cb, None, bytearray(b))

    def state(self, st, rs=0, val=0):
        self.notify(bytes([kf.NTF_STATE, st, rs]) + struct.pack("<I", val))

    async def write_gatt_char(self, uuid, data, response=False):
        data = bytes(data)
        if uuid == kf.CTRL_UUID and data[0] == kf.CMD_ABORT:
            self.aborted = True
            return None
        if uuid == kf.CTRL_UUID:
            code, size = struct.unpack_from("<II", data, 1)
            sha, sig = data[9:41], data[41:105]
            if not ed25519.verify(self.pub, kf.signed_message(code, size, sha), sig):
                return self.state(kf.ST_REFUSED, kf.R_BAD_SIGNATURE)
            if code <= self.my_code:
                return self.state(kf.ST_REFUSED, kf.R_NOT_NEWER)
            self.offer = (code, size, sha)
            self.state(kf.ST_PROMPT, 0, 120)
            if self.decision == "install":
                self.state(kf.ST_READY)
            elif self.decision == "silent":
                pass
            elif self.decision == "later":
                self.state(kf.ST_SNOOZED, 0, kf.SNOOZE_SECS)
            return None
        self.writes += 1
        if self.drop_every and self.writes % self.drop_every == 0:
            return None                            # lost packet
        off = struct.unpack_from("<I", data)[0]
        payload = data[4:]
        if off < len(self.rx):
            return None
        if off > len(self.rx):
            self.notify(bytes([kf.NTF_NACK]) + struct.pack("<I", len(self.rx)))
            return None
        self.rx += payload
        size = self.offer[1]
        if len(self.rx) - self.acked >= 4096 or len(self.rx) == size:
            self.acked = len(self.rx)
            self.notify(bytes([kf.NTF_ACK]) + struct.pack("<I", self.acked))
        if len(self.rx) == size:
            ok = hashlib.sha256(bytes(self.rx)).digest() == self.offer[2]
            self.state(kf.ST_DONE if ok else kf.ST_FAILED, 0 if ok else kf.R_HASH_MISMATCH)
        await asyncio.sleep(0)
        return None


async def test_session_install_streams_image():
    img = bytes([0xE9]) + bytes((i * 7) & 0xFF for i in range(60000))
    b = kf.parse_bundle(bundle_bytes(102, img=img))
    unit = FakeUnit("install")
    ev = await Session(unit, b, say=lambda m: None).run()
    assert ev.state == kf.ST_DONE and bytes(unit.rx) == img


async def test_session_recovers_from_lost_packets():
    img = bytes([0xE9]) + bytes((i * 13) & 0xFF for i in range(30000))
    b = kf.parse_bundle(bundle_bytes(102, img=img))
    unit = FakeUnit("install", drop_every=17)
    ev = await asyncio.wait_for(Session(unit, b, say=lambda m: None).run(), 60)
    assert ev.state == kf.ST_DONE and bytes(unit.rx) == img


async def test_session_later_and_foreign_key():
    b = kf.parse_bundle(bundle_bytes(102))
    ev = await Session(FakeUnit("later"), b, say=lambda m: None).run()
    assert ev.state == kf.ST_SNOOZED and kf.outcome_for(ev) == ("snoozed", float(kf.SNOOZE_SECS))
    other = ed25519.public_key(bytes([9] * 32))       # a unit of another family / signing key
    ev = await Session(FakeUnit("install", pub=other), b, say=lambda m: None).run()
    assert ev.state == kf.ST_REFUSED and ev.reason == kf.R_BAD_SIGNATURE
    ev = await Session(FakeUnit("install", my_code=102), b, say=lambda m: None).run()
    assert ev.reason == kf.R_NOT_NEWER


async def test_session_no_tap_aborts_the_prompt():
    b = kf.parse_bundle(bundle_bytes(102))
    unit = FakeUnit("silent")
    ev = await Session(unit, b, say=lambda m: None, prompt_wait=0.2).run()
    assert ev.state == kf.ST_NOANSWER and getattr(unit, "aborted", False)
    assert kf.outcome_for(ev)[0] == "noanswer"
