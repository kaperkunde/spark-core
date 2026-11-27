"""SpArkKids firmware bundles and the kids-OTA protocol (no Bluetooth code here).

A bundle (`pio run -e SparkKids_TDeckPlus -t kids_ota` -> .pio/build/SparkKids_TDeckPlus/kids-ota.zip)
is a zip with firmware.bin and manifest.json. The manifest carries the version code, the image
size and SHA-256, the family tag and an Ed25519 signature made with KIDS_OTA_SIGNING_KEY (.env).
The T-Deck has the matching public key compiled in and checks the signature before it accepts
an offer, and the image hash before it switches partitions. The bridge only carries the bundle:
it checks it for consistency (so it never offers a broken file) but cannot make one.

The protocol constants here must match examples/spark_kids/KidsLogic.h (kids::ota). This module
is also imported by spark/pio_env.py at build time, so it only uses the standard library.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import ed25519

# --- protocol constants (examples/spark_kids/KidsLogic.h) -------------------------------------

SERVICE_UUID = "8e5f0001-7c5a-4b39-9a0e-53704b694f54"
CTRL_UUID = "8e5f0002-7c5a-4b39-9a0e-53704b694f54"   # write (commands), notify (states), read (status)
DATA_UUID = "8e5f0003-7c5a-4b39-9a0e-53704b694f54"   # write without response: [offset u32][bytes]

COMPANY_ID = 0xFFFF            # advertising manufacturer data (the "no company" test id)
ADV_MAGIC = b"SK"
PROTO = 1

SIGN_DOMAIN = b"SPARK-KIDS-OTA-1"     # 16 bytes; signed message = domain + code + size + sha256
FAMILY_DOMAIN = b"spark-kids-family-1"
SNOOZE_SECS = 12 * 3600
LABEL_MAX = 24

# advert flags
ADV_SNOOZED, ADV_LOW_BATTERY, ADV_BUSY = 0x01, 0x02, 0x04

# commands (bridge -> T-Deck, CTRL)
CMD_OFFER, CMD_FINISH, CMD_ABORT, CMD_QUERY = 0x01, 0x02, 0x03, 0x04
# notifications (T-Deck -> bridge, CTRL)
NTF_STATE, NTF_ACK, NTF_NACK = 0x81, 0x82, 0x83

ST_PROMPT, ST_SNOOZED, ST_REFUSED, ST_READY, ST_DONE, ST_FAILED, ST_NOANSWER, ST_BUSY = range(1, 9)
STATE_NAMES = {ST_PROMPT: "prompt", ST_SNOOZED: "snoozed", ST_REFUSED: "refused", ST_READY: "ready",
               ST_DONE: "done", ST_FAILED: "failed", ST_NOANSWER: "no answer", ST_BUSY: "busy"}

(R_NONE, R_NOT_NEWER, R_BAD_SIGNATURE, R_TOO_BIG, R_LOW_BATTERY, R_BAD_OFFER, R_HASH_MISMATCH,
 R_FLASH_ERROR, R_ABORTED, R_DISABLED, R_TIMEOUT) = range(11)
REASON_NAMES = {R_NONE: "", R_NOT_NEWER: "not newer", R_BAD_SIGNATURE: "bad signature", R_TOO_BIG: "too big",
                R_LOW_BATTERY: "low battery", R_BAD_OFFER: "bad offer", R_HASH_MISMATCH: "hash mismatch",
                R_FLASH_ERROR: "flash error", R_ABORTED: "aborted", R_DISABLED: "disabled", R_TIMEOUT: "timeout"}

MAX_IMAGE = 0x640000   # one OTA slot of the T-Deck's 16 MB partition table (default_16MB.csv)


class BundleError(Exception):
    pass


def signed_message(code: int, size: int, sha256: bytes) -> bytes:
    return SIGN_DOMAIN + struct.pack("<II", code, size) + sha256


def family_tag(room_key: bytes, ota_pubkey: bytes) -> bytes:
    """4 bytes advertised by the T-Deck: which bridge's bundles are meant for it.

    A hash, so the advert does not reveal the node's key (the mesh identity of the room)."""
    return hashlib.sha256(FAMILY_DOMAIN + room_key + ota_pubkey).digest()[:4]


def short_label(version: str) -> str:
    """'SpArkKids1.1 (MeshCore v1.18.0)' -> 'SpArkKids1.1' (at most LABEL_MAX ASCII bytes)."""
    s = (version or "").split(" ")[0]
    return s.encode("ascii", "replace")[:LABEL_MAX].decode("ascii")


# --- bundles -----------------------------------------------------------------------------------

@dataclass
class Bundle:
    code: int
    version: str
    size: int
    sha256: bytes
    pubkey: bytes
    family: bytes
    sig: bytes
    image: bytes = field(repr=False)
    board: str = "tdeck-plus"

    @property
    def label(self) -> str:
        return short_label(self.version)

    def offer(self) -> bytes:
        return encode_offer(self.code, self.size, self.sha256, self.sig, self.label)


def make_bundle(image: bytes, code: int, version: str, seed: bytes, room_key: bytes) -> bytes:
    """Sign an image: returns the bundle zip as bytes."""
    if not 0 < code < 2**32:
        raise BundleError("version code must be 1 .. 2^32-1")
    if not image or len(image) > MAX_IMAGE:
        raise BundleError(f"image size {len(image)} is not 1 .. {MAX_IMAGE}")
    if len(room_key) != 32:
        raise BundleError("room key must be 32 bytes")
    sha = hashlib.sha256(image).digest()
    pub = ed25519.public_key(seed)
    sig = ed25519.sign(seed, signed_message(code, len(image), sha))
    manifest = {
        "format": 1, "component": "spark-kids", "board": "tdeck-plus", "code": code, "version": version,
        "size": len(image), "sha256": sha.hex(), "pubkey": pub.hex(), "family": family_tag(room_key, pub).hex(),
        "sig": sig.hex(),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=1) + "\n")
        z.writestr("firmware.bin", image)
    return buf.getvalue()


def _hex(m: dict, key: str, n: int) -> bytes:
    try:
        b = bytes.fromhex(str(m[key]))
    except (KeyError, ValueError) as exc:
        raise BundleError(f"manifest: {key} missing or not hex") from exc
    if len(b) != n:
        raise BundleError(f"manifest: {key} must be {n} bytes")
    return b


def parse_bundle(data: bytes) -> Bundle:
    """Check a bundle (zip bytes) and return it. Raises BundleError."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            m = json.loads(z.read("manifest.json"))
            image = z.read("firmware.bin")
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BundleError(f"not a SpArkKids bundle: {exc}") from exc
    if m.get("format") != 1 or m.get("component") != "spark-kids":
        raise BundleError("not a SpArkKids bundle (format/component)")
    try:
        code, size = int(m["code"]), int(m["size"])
    except (KeyError, ValueError, TypeError) as exc:
        raise BundleError("manifest: code/size missing") from exc
    b = Bundle(code=code, version=str(m.get("version", "")), size=size, sha256=_hex(m, "sha256", 32),
               pubkey=_hex(m, "pubkey", 32), family=_hex(m, "family", 4), sig=_hex(m, "sig", 64), image=image,
               board=str(m.get("board", "")))
    if b.board != "tdeck-plus":
        raise BundleError(f"bundle is for board {b.board!r}, not tdeck-plus")
    if not 0 < code < 2**32 or size != len(image) or not 0 < size <= MAX_IMAGE:
        raise BundleError("manifest: code or size out of range / size does not match firmware.bin")
    if hashlib.sha256(image).digest() != b.sha256:
        raise BundleError("firmware.bin does not match the manifest's sha256")
    if image[0] != 0xE9:
        raise BundleError("firmware.bin is not an ESP32 app image")
    if not ed25519.verify(b.pubkey, signed_message(code, size, b.sha256), b.sig):
        raise BundleError("bad signature")
    return b


def load_bundle(path: str | Path) -> Bundle:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise BundleError(f"cannot read {path}: {exc}") from exc
    return parse_bundle(data)


CURRENT = "current.zip"


def install_bundle(src: str | Path, fw_dir: str | Path, *, force: bool = False) -> Bundle:
    """Copy a checked bundle to <fw_dir>/current.zip. A lower code than the installed one needs force."""
    b = load_bundle(src)
    d = Path(fw_dir)
    d.mkdir(parents=True, exist_ok=True)
    cur = d / CURRENT
    if cur.is_file() and not force:
        try:
            old = load_bundle(cur)
        except BundleError:
            old = None
        if old and old.code > b.code:
            raise BundleError(f"installed bundle has a higher code ({old.code} > {b.code}); use --force")
    tmp = d / (CURRENT + ".tmp")
    tmp.write_bytes(Path(src).read_bytes())
    tmp.replace(cur)
    return b


# --- wire formats ------------------------------------------------------------------------------

@dataclass
class Advert:
    family: bytes
    code: int
    flags: int

    @property
    def snoozed(self) -> bool:
        return bool(self.flags & ADV_SNOOZED)

    @property
    def low_battery(self) -> bool:
        return bool(self.flags & ADV_LOW_BATTERY)

    @property
    def busy(self) -> bool:
        return bool(self.flags & ADV_BUSY)


def encode_advert(family: bytes, code: int, flags: int) -> bytes:
    """Manufacturer data payload after the company id (as bleak reports it)."""
    return ADV_MAGIC + bytes([PROTO]) + family + struct.pack("<IB", code, flags)


def parse_advert(manufacturer_data: dict[int, bytes]) -> Advert | None:
    p = (manufacturer_data or {}).get(COMPANY_ID)
    if not p or len(p) < 12 or p[:2] != ADV_MAGIC or p[2] != PROTO:
        return None
    code, flags = struct.unpack_from("<IB", p, 7)
    return Advert(family=bytes(p[3:7]), code=code, flags=flags)


def encode_offer(code: int, size: int, sha256: bytes, sig: bytes, label: str) -> bytes:
    lab = label.encode("ascii", "replace")[:LABEL_MAX]
    return bytes([CMD_OFFER]) + struct.pack("<II", code, size) + sha256 + sig + lab


@dataclass
class Status:
    proto: int
    code: int
    flags: int
    snooze_secs: int
    batt_mv: int
    label: str


def parse_status(b: bytes) -> Status | None:
    if len(b) < 12:
        return None
    proto, code, flags, snooze, mv = struct.unpack_from("<BIBIH", b, 0)
    return Status(proto, code, flags, snooze, mv, bytes(b[12:]).split(b"\0")[0].decode("ascii", "replace"))


@dataclass
class Event:
    kind: int            # NTF_STATE / NTF_ACK / NTF_NACK
    state: int = 0
    reason: int = 0
    value: int = 0       # STATE: extra value (seconds); ACK/NACK: offset

    def describe(self) -> str:
        if self.kind == NTF_STATE:
            r = REASON_NAMES.get(self.reason, str(self.reason))
            return f"{STATE_NAMES.get(self.state, self.state)}{' (' + r + ')' if r else ''} {self.value}"
        return f"{'ack' if self.kind == NTF_ACK else 'nack'} {self.value}"


def parse_event(b: bytes) -> Event | None:
    if len(b) >= 7 and b[0] == NTF_STATE:
        st, rs, val = struct.unpack_from("<BBI", b, 1)
        return Event(NTF_STATE, st, rs, val)
    if len(b) >= 5 and b[0] in (NTF_ACK, NTF_NACK):
        return Event(b[0], value=struct.unpack_from("<I", b, 1)[0])
    return None


def data_packet(offset: int, chunk: bytes) -> bytes:
    return struct.pack("<I", offset) + chunk


# --- when to (re)offer ------------------------------------------------------------------------

# Back-off after each outcome, seconds. The unit's own snooze (12 h at most) wins when it says so.
BACKOFF = {
    "snoozed": SNOOZE_SECS,
    "noanswer": 3600,          # nobody tapped: try again in an hour
    "low_battery": 1800,
    "busy": 300,
    "refused": 24 * 3600,      # not newer / bad signature / too big: only a new bundle helps
    "done": 600,               # the unit reboots; its advert shows the new code afterwards
}
CONNECT_BACKOFF_START, CONNECT_BACKOFF_MAX = 60.0, 1800.0


@dataclass
class _Unit:
    next_try: float = 0.0
    fails: int = 0
    bundle_code: int = 0


class OfferPolicy:
    """Per unit (BLE address) back-off so the bridge does not reconnect all the time."""

    def __init__(self):
        self._units: dict[str, _Unit] = {}

    def should_offer(self, addr: str, adv: Advert, bundle: Bundle, now: float) -> tuple[bool, str]:
        if adv.family != bundle.family:
            return False, "other family"
        if adv.code >= bundle.code:
            return False, "up to date"
        if adv.snoozed:
            return False, "snoozed (advert)"
        if adv.low_battery:
            return False, "low battery (advert)"
        if adv.busy:
            return False, "busy (advert)"
        u = self._units.get(addr)
        if u and u.bundle_code == bundle.code and now < u.next_try:
            return False, f"backing off {int(u.next_try - now)} s"
        return True, "offer"

    def record(self, addr: str, outcome: str, bundle: Bundle, now: float, seconds: float | None = None) -> float:
        """Outcome: a BACKOFF key, or 'error' (connection/transfer failure, exponential).
        `seconds` (e.g. the unit's remaining snooze) overrides the table. Returns the next try time."""
        u = self._units.setdefault(addr, _Unit())
        if u.bundle_code != bundle.code:
            u.fails = 0
        u.bundle_code = bundle.code
        if outcome == "error":
            u.fails += 1
            wait = min(CONNECT_BACKOFF_START * 2 ** (u.fails - 1), CONNECT_BACKOFF_MAX)
        else:
            u.fails = 0
            wait = BACKOFF[outcome]
            if seconds is not None:
                wait = max(60.0, min(float(seconds), float(BACKOFF[outcome])))
        u.next_try = now + wait
        return u.next_try


def outcome_for(ev: Event) -> tuple[str, float | None]:
    """Policy outcome (and an override in seconds) for the final state of an offer."""
    if ev.state == ST_SNOOZED:
        return "snoozed", float(ev.value) if ev.value else None
    if ev.state == ST_NOANSWER:
        return "noanswer", None
    if ev.state == ST_BUSY:
        return "busy", None
    if ev.state == ST_DONE:
        return "done", None
    if ev.reason == R_LOW_BATTERY:
        return "low_battery", None
    if ev.state == ST_REFUSED:
        return "refused", None
    return "error", None
