"""Contract-faithful fake HostLink node (TCP, serial-style framing).

Implements HOSTLINK CONTRACT v1 (plus the SparkNode1.2 sender name on channel sends, when the
stub's version is SparkNode1.2 or later): the frame subset, the room-post replay cursor, the
32-entry channel ring, "no echo of host posts" and the golden vectors. Used by the tests
and runnable by hand:

    python -m spark_bridge.stubnode --port 5000

stdin commands: chan <Sender>: <text> | post <author-hex8> <text> | advert <name> |
                drop | state | quit
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass

from .textlimits import sanitize_sender

OWN_PUBKEY = bytes.fromhex("a1b2c3d4e5f6") + bytes(range(26))
PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")

# frame codes
OK, ERR, CONTACTS_START, CONTACT, END_OF_CONTACTS, SELF_INFO, SENT = 0, 1, 2, 3, 4, 5, 6
CURR_TIME, NO_MORE, BATT, DEVICE_INFO, ROOM_POST, CHAN_MSG, CHANNEL_INFO = 9, 10, 12, 13, 16, 17, 18
STATS, CLI_REPLY = 24, 29
PUSH_MSG_WAITING, PUSH_NEW_ADVERT = 0x83, 0x8A


def le32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def pad(b: bytes | str, n: int) -> bytes:
    if isinstance(b, str):
        b = b.encode()
    return b[:n].ljust(n, b"\0")


def cut_utf8(text: str, n: int) -> str:
    return text.encode()[:n].decode(errors="ignore")


# --- frame builders (payloads, no transport header) --------------------------------
def err(code: int) -> bytes:
    return bytes([ERR, code])


def build_channel_msg(text: str, ts: int, snr: float = 0.0, path_len: int = 0xFF, idx: int = 0) -> bytes:
    return (bytes([CHAN_MSG]) + struct.pack("<b", int(round(snr * 4))) + b"\0\0"
            + bytes([idx, path_len, 0]) + le32(ts) + text.encode())


def build_room_post(author4: bytes, text: str, ts: int, room_prefix: bytes = OWN_PUBKEY[:6]) -> bytes:
    return (bytes([ROOM_POST]) + b"\x00\x00\x00" + room_prefix + b"\xff\x02" + le32(ts)
            + author4 + text.encode())


def build_stats(kind: int, **kw) -> bytes:
    if kind == 0:
        return bytes([STATS, 0]) + struct.pack("<HIHB", kw["batt"], kw["uptime"], kw.get("errs", 0), kw.get("q", 0))
    if kind == 1:
        return bytes([STATS, 1]) + struct.pack("<hbbII", kw["noise"], kw["rssi"], int(round(kw["snr"] * 4)),
                                               kw["tx"], kw["rx"])
    return bytes([STATS, 2]) + struct.pack("<IIIIIII", *kw["counters"])


def build_battery(mv: int, used: int = 0, total: int = 0) -> bytes:
    return bytes([BATT]) + struct.pack("<HII", mv, used, total)


def build_contact(code: int, pubkey: bytes, name: str, adv_type: int = 1, ts: int = 0, lastmod: int = 0) -> bytes:
    return (bytes([code]) + pubkey + bytes([adv_type, 0, 0xFF]) + bytes(64) + pad(name, 32)
            + le32(ts) + struct.pack("<ii", 0, 0) + le32(lastmod))


def build_self_info(name: str = "SparkNode") -> bytes:
    return (bytes([SELF_INFO, 3, 22, 22]) + OWN_PUBKEY + struct.pack("<ii", 0, 0) + bytes([0, 0, 0, 0])
            + le32(869525) + le32(250000) + bytes([11, 5]) + name.encode())


STUB_VERSION = "v1.18.0-SparkNode1.2"


def build_device_info(version: str = STUB_VERSION, repeat: bool = True) -> bytes:
    return (bytes([DEVICE_INFO, 14, 0, 1]) + le32(0) + pad("20 Nov 2026", 12) + pad("SparkNode stub", 40)
            + pad(version, 20) + bytes([int(repeat), 0]))


def frame_out(payload: bytes) -> bytes:
    return b">" + struct.pack("<H", len(payload)) + payload


@dataclass
class Post:
    ts: int
    author4: bytes
    text: str


class StubNode:
    def __init__(self, name: str = "SparkNode", version: str = STUB_VERSION, clock=time.time):
        self.name = name
        self.version = version
        self.clock = clock
        self.clock_offset = 0
        self.battery_mv = 4012
        self.uptime = 86400
        self.noise, self.rssi, self.snr = -112, -95, 7.5
        self.tx_air, self.rx_air = 120, 3600
        self.packets = (10, 5, 3, 2, 7, 3, 0)
        self.posts: list[Post] = []
        self.cursor = 0
        self.skip: set[int] = set()  # host-originated posts: not echoed until the next APP_START
        self.ring: deque[bytes] = deque(maxlen=32)
        self.contacts: list[tuple[bytes, str, int]] = []
        self.cli_replies = {"get repeat": "> on", "ver": "v1.18.0-SparkNode1"}
        # test hooks
        self.received: list[bytes] = []
        self.transmitted: list[str] = []  # channel text as sent on air
        self.room_pushed: list[str] = []  # posts the room pushes to mesh clients
        self.cli_log: list[str] = []
        self.refuse_frames = False  # when True: swallow commands without replying
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    # --- lifecycle ---------------------------------------------------------
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        await self.drop_connections()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_connections(self) -> None:
        for w in list(self._writers):
            w.close()
        self._writers.clear()
        await asyncio.sleep(0)

    @property
    def supports_sender(self) -> bool:
        from .nodelink import SENDER_NAME_VERSION, spark_node_version
        v = spark_node_version(self.version)
        return v is not None and v >= SENDER_NAME_VERSION

    @property
    def connected(self) -> bool:
        return bool(self._writers)

    # --- node clock --------------------------------------------------------
    def now(self) -> int:
        return int(self.clock()) + self.clock_offset

    # --- injection hooks -----------------------------------------------------
    def inject_channel(self, sender: str, text: str, ts: int | None = None, snr: float = 5.0, hops: int = 2) -> None:
        ts = self.now() if ts is None else ts
        self.ring.append(build_channel_msg(f"{sender}: {text}", ts, snr, hops))
        self._push(bytes([PUSH_MSG_WAITING]))

    def inject_room_post(self, author4: bytes | str, text: str, ts: int | None = None) -> None:
        a = bytes.fromhex(author4) if isinstance(author4, str) else author4
        self.posts.append(Post(self.now() if ts is None else ts, a, text))
        self._push(bytes([PUSH_MSG_WAITING]))

    def inject_advert(self, name: str, pubkey: bytes | None = None, adv_type: int = 1) -> bytes:
        pubkey = pubkey or (bytes([len(self.contacts) + 1]) * 4 + bytes(28))
        self.contacts.append((pubkey, name, adv_type))
        self._push(build_contact(PUSH_NEW_ADVERT, pubkey, name, adv_type, self.now()))
        return pubkey

    # --- IO ----------------------------------------------------------------
    def _push(self, payload: bytes) -> None:
        for w in list(self._writers):
            if not w.is_closing():
                w.write(frame_out(payload))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        buf = bytearray()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                while True:
                    i = buf.find(b"<")
                    if i < 0:
                        buf.clear()
                        break
                    del buf[:i]
                    if len(buf) < 3:
                        break
                    n = buf[1] | (buf[2] << 8)
                    if len(buf) < 3 + n:
                        break
                    payload = bytes(buf[3:3 + n])
                    del buf[:3 + n]
                    self.received.append(payload)
                    if self.refuse_frames:
                        continue
                    for reply in self.handle_frame(payload):
                        writer.write(frame_out(reply))
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    # --- the contract ------------------------------------------------------
    def _pending_post(self) -> int | None:
        for i in range(self.cursor, len(self.posts)):
            if i not in self.skip:
                return i
        return None

    def _waiting(self) -> bool:
        return self._pending_post() is not None or bool(self.ring)

    def handle_frame(self, p: bytes) -> list[bytes]:
        """Return reply payloads (in order) for one command frame."""
        if not p:
            return [err(1)]
        c = p[0]
        if c == 1:  # APP_START
            self.cursor = 0
            self.skip.clear()
            out = [build_self_info(self.name)]
            if self._waiting():
                out.append(bytes([PUSH_MSG_WAITING]))
            return out
        if c == 22:
            return [build_device_info(self.version)]
        if c == 5:
            return [bytes([CURR_TIME]) + le32(self.now())]
        if c == 6:
            if len(p) < 5:
                return [err(6)]
            v = struct.unpack("<I", p[1:5])[0]
            if v < self.now():
                return [err(6)]
            self.clock_offset += v - self.now()
            return [bytes([OK])]
        if c == 20:
            return [build_battery(self.battery_mv)]
        if c == 56:
            if len(p) < 2 or p[1] > 2:
                return [err(6)]
            if p[1] == 0:
                return [build_stats(0, batt=self.battery_mv, uptime=self.uptime)]
            if p[1] == 1:
                return [build_stats(1, noise=self.noise, rssi=self.rssi, snr=self.snr, tx=self.tx_air, rx=self.rx_air)]
            return [build_stats(2, counters=self.packets)]
        if c == 31:
            if len(p) < 2 or p[1] != 0:
                return [err(2)]
            return [bytes([CHANNEL_INFO, 0]) + pad("Public", 32) + PUBLIC_SECRET]
        if c == 32:
            return [err(1)]
        if c == 3:
            if len(p) < 7:
                return [err(6)]
            if p[1] != 0:
                return [err(1)]
            if p[2] != 0:
                return [err(2)]
            raw, _, trailer = p[7:].partition(b"\0")
            text = raw.decode(errors="ignore")
            sender = ""
            if trailer and self.supports_sender:
                sender = sanitize_sender(trailer.partition(b"\0")[0].decode(errors="ignore"))
            self.transmitted.append(cut_utf8(f"{sender or self.name}: {text}", 160))
            return [bytes([OK])]
        if c == 2:
            if len(p) < 13:
                return [err(6)]
            if p[1] != 0:
                return [err(1)]
            if p[7:13] != OWN_PUBKEY[:6]:
                return [err(2)]
            text = cut_utf8(p[13:].decode(errors="ignore"), 151)
            self.posts.append(Post(self.now(), OWN_PUBKEY[:4], text))
            self.skip.add(len(self.posts) - 1)
            self.room_pushed.append(text)
            return [bytes([SENT, 0]) + le32(0) + le32(0)]
        if c == 66:
            cmd = p[1:].decode(errors="ignore")
            self.cli_log.append(cmd)
            if cmd.startswith("set ble.pin"):
                return [bytes([CLI_REPLY]) + b"ERR: not allowed here"]
            return [bytes([CLI_REPLY]) + self.cli_replies.get(cmd, "OK").encode()]
        if c == 10:
            i = self._pending_post()
            if i is not None:
                self.cursor = i + 1
                post = self.posts[i]
                return [build_room_post(post.author4, post.text, post.ts)]
            if self.ring:
                return [self.ring.popleft()]
            return [bytes([NO_MORE])]
        if c == 4:
            out = [bytes([CONTACTS_START]) + le32(len(self.contacts))]
            for pk, name, t in self.contacts:
                out.append(build_contact(CONTACT, pk, name, t, self.now(), 1))
            out.append(bytes([END_OF_CONTACTS]) + le32(1 if self.contacts else 0))
            return out
        return [err(1)]


async def _stdin_loop(node: StubNode) -> None:
    loop = asyncio.get_running_loop()
    print("commands: chan Sender: text | post <hex8> text | advert <name> | drop | state | quit", flush=True)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(3600)
            continue
        line = line.strip()
        cmd, _, rest = line.partition(" ")
        if cmd == "chan" and ": " in rest:
            sender, _, text = rest.partition(": ")
            node.inject_channel(sender, text)
        elif cmd == "post":
            author, _, text = rest.partition(" ")
            node.inject_room_post(author, text)
        elif cmd == "advert":
            node.inject_advert(rest or "someone")
        elif cmd == "drop":
            await node.drop_connections()
        elif cmd == "state":
            print(f"posts={len(node.posts)} ring={len(node.ring)} transmitted={node.transmitted} "
                  f"room_pushed={node.room_pushed}", flush=True)
        elif cmd == "quit":
            return


async def _amain(args) -> None:
    node = StubNode(name=args.name)
    port = await node.start(args.host, args.port)
    print(f"stub node '{node.name}' listening on {args.host}:{port}", flush=True)
    if args.seconds:
        await asyncio.sleep(args.seconds)
    else:
        await _stdin_loop(node)
    await node.stop()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Fake HostLink node (TCP) for testing spark-bridge")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--name", default="SparkNode")
    ap.add_argument("--seconds", type=float, default=0, help="run for N seconds without stdin, then exit")
    try:
        asyncio.run(_amain(ap.parse_args(argv)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
