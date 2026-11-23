"""MQTT observer: publish every packet the node's radio receives, in the meshcoretomqtt format.

This makes the station an observer for the DutchMeshCore collectors (MC-Radar) or Let's Mesh,
"option 2" of meshwiki.nl/wiki/Observer: the node has no WiFi, so it hands raw received packets
to the bridge (HostLink LOG_RX_DATA, SpArkNode1.3) and the bridge publishes them.

Format (Cisien/meshcoretomqtt, agessaman/meshcore-packet-capture):
  topic    meshcore/<IATA>/<PUBKEY>/packets   one JSON object per received packet (not retained)
           meshcore/<IATA>/<PUBKEY>/status    {"status": "online"|"offline", ...} (retained, LWT)
  auth     username "v1_<PUBKEY>", password a JWT-like token:
           b64url({"alg":"Ed25519","typ":"JWT"}) "." b64url(claims) "." hex(Ed25519 signature)
           claims publicKey, iat, exp, aud (broker host), optional owner/email, client.
The signature is made BY THE NODE (HostLink SIGN_*, which only signs a JWT signing input), so
the node's private key never leaves it. PUBKEY is the node's 64-hex public key, upper case.

aiomqtt is imported only when the observer runs, so a bridge without it still works.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from . import __version__

log = logging.getLogger("spark_bridge.observer")

CLIENT = f"spark-bridge/{__version__}"
ROUTE_LETTERS = {0: "F", 1: "F", 2: "D", 3: "T"}   # as meshcore-packet-capture maps them
PAYLOAD_TRACE = 9


class ObserverError(Exception):
    pass


@dataclass
class Broker:
    name: str
    host: str
    port: int = 443
    transport: str = "websockets"   # websockets | tcp
    tls: bool = True
    auth: str = "token"             # token | password | none
    ws_path: str | None = None      # None: ObserverConfig.ws_path

    @property
    def audience(self) -> str:
        return self.host


PRESETS: dict[str, list[Broker]] = {
    "dutchmeshcore-1": [Broker("dutchmeshcore-1", "collector1.dutchmeshcore.nl")],
    "dutchmeshcore-2": [Broker("dutchmeshcore-2", "collector2.dutchmeshcore.nl")],
    "letsmesh-eu": [Broker("letsmesh-eu", "mqtt-eu-v1.letsmesh.net")],
    "letsmesh-us": [Broker("letsmesh-us", "mqtt-us-v1.letsmesh.net")],
}
PRESETS["dutchmeshcore"] = PRESETS["dutchmeshcore-1"] + PRESETS["dutchmeshcore-2"]
PRESETS["letsmesh"] = PRESETS["letsmesh-eu"]


@dataclass
class ObserverConfig:
    enabled: bool = False
    # preset names (dutchmeshcore, dutchmeshcore-1/-2, letsmesh-eu/-us) or URLs
    # mqtt://host:1883, mqtts://host:8883, ws://host:80/path, wss://host:443/path
    brokers: list[str] = field(default_factory=lambda: ["dutchmeshcore"])
    iata: str = ""             # region code in the topic, e.g. AMS
    name: str = ""             # "origin" in the JSON; empty = the node's name
    owner: str = ""            # optional token claim: your companion's public key (64 hex)
    email: str = ""            # optional token claim
    auth: str = ""             # URL brokers: token | password | none (default: password with a username, else token)
    username: str = ""         # password auth
    password_env: str = ""     # NAME of the environment variable that holds the password
    ws_path: str = "/"         # websocket path (meshcoretomqtt uses "/"; some brokers document /mqtt)
    tls_verify: bool = True
    token_minutes: int = 60
    queue_size: int = 1000     # per broker; the oldest packets are dropped when it is full
    status_minutes: int = 5


def parse_brokers(cfg: ObserverConfig) -> list[Broker]:
    out: list[Broker] = []
    for spec in cfg.brokers:
        spec = spec.strip()
        if not spec:
            continue
        if spec.lower() in PRESETS:
            out.extend(Broker(**vars(b)) for b in PRESETS[spec.lower()])
            continue
        u = urlsplit(spec if "://" in spec else f"mqtts://{spec}")
        if u.scheme not in ("mqtt", "mqtts", "ws", "wss") or not u.hostname:
            raise ObserverError(f"broker {spec!r}: use a preset ({', '.join(sorted(PRESETS))}) "
                                "or mqtt://, mqtts://, ws://, wss:// URL")
        tls = u.scheme in ("mqtts", "wss")
        transport = "websockets" if u.scheme in ("ws", "wss") else "tcp"
        port = u.port or {"mqtt": 1883, "mqtts": 8883, "ws": 80, "wss": 443}[u.scheme]
        auth = cfg.auth or ("password" if cfg.username else "token")
        out.append(Broker(u.hostname, u.hostname, port, transport, tls, auth, u.path or None))
    if not out:
        raise ObserverError("no brokers configured")
    for b in out:
        if b.auth not in ("token", "password", "none"):
            raise ObserverError(f"broker {b.name}: auth must be token, password or none")
    return out


# --- packet format ---------------------------------------------------------------------------

def decode_packet(raw: bytes) -> dict[str, Any]:
    """Header fields of a raw MeshCore packet (as Packet::readFrom / meshcore-packet-capture)."""
    if len(raw) < 2:
        raise ValueError("packet too short")
    h = raw[0]
    route_bits = h & 3
    ptype = (h >> 2) & 0x0F
    off = 1 + (4 if route_bits in (0, 3) else 0)   # transport codes
    if off >= len(raw):
        raise ValueError("truncated")
    pl = raw[off]
    off += 1
    hops, size = pl & 0x3F, (pl >> 6) + 1
    if size == 4 or hops * size > 64:
        raise ValueError("bad path length")
    path = raw[off:off + hops * size]
    if len(path) < hops * size:
        raise ValueError("truncated path")
    payload = raw[off + hops * size:]
    sha = hashlib.sha256(bytes([ptype]))
    if ptype == PAYLOAD_TRACE:
        sha.update(pl.to_bytes(2, "little"))   # Packet::path_len is a uint16_t
    sha.update(payload)
    return {"route_bits": route_bits, "route": ROUTE_LETTERS[route_bits], "packet_type": ptype,
            "path_len": pl, "hop_size": size, "path": path, "payload_len": len(payload),
            "hash": sha.hexdigest()[:16].upper()}


def packet_message(raw: bytes, snr: float, rssi: int, when: float, origin: str, origin_id: str) -> dict[str, Any]:
    """One /packets JSON object. Numbers are strings, as meshcoretomqtt sends them."""
    dt = datetime.fromtimestamp(when, timezone.utc)
    msg: dict[str, Any] = {"origin": origin, "origin_id": origin_id, "timestamp": dt.isoformat(),
                           "type": "PACKET", "direction": "rx", "time": dt.strftime("%H:%M:%S"),
                           "date": dt.strftime("%d/%m/%Y"), "len": str(len(raw))}
    try:
        d = decode_packet(raw)
    except ValueError:
        msg.update({"packet_type": "0", "route": "U", "payload_len": "0"})
        d = None
    else:
        msg.update({"packet_type": str(d["packet_type"]), "route": d["route"], "payload_len": str(d["payload_len"])})
    msg.update({"raw": raw.hex().upper(), "SNR": str(snr), "RSSI": str(rssi),
                "hash": d["hash"] if d else "0000000000000000"})
    if d and d["route"] == "D" and d["path"]:
        n = d["hop_size"]
        msg["path"] = ",".join(d["path"][i:i + n].hex().upper() for i in range(0, len(d["path"]), n))
    return msg


# --- auth token -----------------------------------------------------------------------------

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def token_signing_input(pubkey_hex: str, audience: str, iat: int, ttl: int, owner: str = "",
                        email: str = "") -> str:
    claims: dict[str, Any] = {"publicKey": pubkey_hex.upper(), "iat": iat, "exp": iat + ttl, "aud": audience}
    if owner:
        claims["owner"] = owner.upper()
    if email:
        claims["email"] = email.lower()
    claims["client"] = CLIENT
    header = _b64u(json.dumps({"alg": "Ed25519", "typ": "JWT"}, separators=(",", ":")).encode())
    return header + "." + _b64u(json.dumps(claims, separators=(",", ":")).encode())


def verify_token(token: str, pubkey_hex: str) -> bool:
    """Check the signature against the node's public key (catches a signer/key mismatch early)."""
    from Crypto.Signature import eddsa

    si, _, sig = token.rpartition(".")
    try:
        key = eddsa.import_public_key(bytes.fromhex(pubkey_hex))
        eddsa.new(key, "rfc8032").verify(si.encode(), bytes.fromhex(sig))
        return True
    except (ValueError, TypeError):
        return False


# --- publisher ------------------------------------------------------------------------------

class BrokerState:
    def __init__(self, broker: Broker, queue_size: int):
        self.broker = broker
        self.queue: deque[str] = deque(maxlen=queue_size)
        self.wake = asyncio.Event()
        self.connected = False
        self.sent = 0
        self.dropped = 0
        self.last_error = ""
        self.retry_at: float | None = None
        self.last_publish: float | None = None

    def put(self, payload: str) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1   # deque drops the oldest
        self.queue.append(payload)
        self.wake.set()


class Observer:
    """Feeds LOG_RX_DATA packets to MQTT brokers. `node` is a NodeLink (pubkey, node_name,
    version, connected, sign()); `client_factory(**kwargs)` builds an aiomqtt.Client-like async
    context manager (tests pass a fake)."""

    def __init__(self, cfg: ObserverConfig, node, *, password: str = "", client_factory: Callable | None = None,
                 clock=time.time, sleep=asyncio.sleep, rng: random.Random | None = None):
        if not cfg.iata.strip():
            raise ObserverError("observer IATA/region code is required (e.g. AMS)")
        self.cfg = cfg
        self.iata = cfg.iata.strip().upper()
        self.node = node
        self.password = password
        self.brokers = [BrokerState(b, cfg.queue_size) for b in parse_brokers(cfg)]
        for st in self.brokers:
            if st.broker.auth == "password" and not (cfg.username and password):
                raise ObserverError(f"broker {st.broker.name}: password auth needs a username and the "
                                    "password in the variable named by the password_env setting")
        self._factory = client_factory
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.Random()
        self._stop = asyncio.Event()
        self.packets = 0
        self.sample: dict[str, Any] = {}
        self._tokens: dict[str, tuple[str, float]] = {}

    # ---- identity -------------------------------------------------------------------------
    @property
    def pubkey(self) -> str:
        return (self.node.pubkey or "").upper()

    @property
    def origin(self) -> str:
        return self.cfg.name or self.node.node_name or "SparkNode"

    def topic(self, kind: str) -> str:
        return f"meshcore/{self.iata}/{self.pubkey}/{kind}"

    # ---- feed (called by NodeLink for every LOG_RX_DATA push; sync) ------------------------
    def on_rx(self, pkt) -> None:
        if not self.pubkey:
            return
        self.packets += 1
        payload = json.dumps(packet_message(pkt.raw, pkt.snr, pkt.rssi, pkt.recv_time, self.origin, self.pubkey),
                             separators=(",", ":"))
        for st in self.brokers:
            st.put(payload)

    def record_sample(self, sample: dict[str, Any]) -> None:
        self.sample = dict(sample)

    # ---- status ---------------------------------------------------------------------------
    def status_message(self, status: str) -> str:
        msg: dict[str, Any] = {"status": status,
                               "timestamp": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(),
                               "origin": self.origin, "origin_id": self.pubkey,
                               "model": "SenseCAP Solar P1 (SpArkNode)", "firmware_version": self.node.version,
                               "client_version": CLIENT}
        radio = getattr(self.node, "radio", "")
        if radio:
            msg["radio"] = radio
        s = self.sample
        if status == "online" and s:
            msg["stats"] = {k2: s[k] for k, k2 in (("battery_mv", "battery_mv"), ("uptime_secs", "uptime_secs"),
                                                    ("errors", "debug_flags"), ("queue_len", "queue_len"),
                                                    ("noise_floor", "noise_floor"), ("tx_air_secs", "tx_air_secs"),
                                                    ("rx_air_secs", "rx_air_secs")) if k in s}
        return json.dumps(msg, separators=(",", ":"))

    def summary(self) -> str:
        feed = getattr(self.node, "features", 0) & 1
        parts = []
        for st in self.brokers:
            if st.connected:
                state = "connected"
            elif st.retry_at is not None:
                state = f"retry in {max(0, int(st.retry_at - self._clock()))} s"
            else:
                state = "connecting"
            err = f", last error: {st.last_error}" if st.last_error and not st.connected else ""
            parts.append(f"{st.broker.name} {state} (sent {st.sent}, dropped {st.dropped}{err})")
        if not getattr(self.node, "connected", False):
            node = "waiting (link down)"
        else:
            node = "on" if feed else "off (the node needs SpArkNode1.3)"
        return (f"on, region {self.iata}, raw RX feed {node}, {self.packets} packets; " + "; ".join(parts))

    # ---- credentials ----------------------------------------------------------------------
    async def credentials(self, broker: Broker) -> tuple[str | None, str | None]:
        if broker.auth == "none":
            return None, None
        if broker.auth == "password":
            return self.cfg.username, self.password
        now = self._clock()
        cached = self._tokens.get(broker.audience)
        if cached and cached[1] - now > 300:
            return f"v1_{self.pubkey}", cached[0]
        ttl = max(10, self.cfg.token_minutes) * 60
        si = token_signing_input(self.pubkey, broker.audience, int(now), ttl, self.cfg.owner, self.cfg.email)
        sig = await self.node.sign(si.encode())
        token = f"{si}.{sig.hex().upper()}"
        if not verify_token(token, self.pubkey):
            raise ObserverError("the node's signature does not match its public key")
        self._tokens[broker.audience] = (token, now + ttl)
        return f"v1_{self.pubkey}", token

    def token_expiry(self, broker: Broker) -> float | None:
        t = self._tokens.get(broker.audience) if broker.auth == "token" else None
        return t[1] if t else None

    # ---- run ------------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        for st in self.brokers:
            st.wake.set()

    async def run(self) -> None:
        log.info("MQTT observer: region %s, brokers %s", self.iata, ", ".join(s.broker.name for s in self.brokers))
        await asyncio.gather(*(self._broker_loop(st) for st in self.brokers))

    def _client(self, st: BrokerState, username, password):
        b = st.broker
        kw: dict[str, Any] = dict(hostname=b.host, port=b.port, username=username, password=password,
                                  identifier=("meshcore_" + self.pubkey)[:23], keepalive=60, clean_session=True,
                                  transport=b.transport)
        if b.transport == "websockets":
            kw["websocket_path"] = b.ws_path or self.cfg.ws_path
        if b.tls:
            ctx = ssl.create_default_context()
            if not self.cfg.tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kw["tls_context"] = ctx
        if self._factory is not None:
            return self._factory(**kw, will=(self.topic("status"), self.status_message("offline")))
        import aiomqtt

        return aiomqtt.Client(**kw, will=aiomqtt.Will(self.topic("status"), self.status_message("offline"),
                                                      qos=0, retain=True))

    async def _broker_loop(self, st: BrokerState) -> None:
        delay = 2.0
        while not self._stop.is_set():
            if not self.pubkey or not getattr(self.node, "connected", False):
                await self._sleep(1.0)   # the token needs the node; the topic needs its key
                continue
            v = getattr(self.node, "node_version", None)
            if v is not None and v < (1, 3):
                await self._sleep(5.0)   # no raw RX feed and no signing before SpArkNode1.3
                continue
            started = self._clock()
            try:
                user, pw = await self.credentials(st.broker)
                async with self._client(st, user, pw) as client:
                    st.connected, st.last_error, st.retry_at = True, "", None
                    log.info("observer: connected to %s:%d (ws path %s), publishing to %s and %s",
                             st.broker.host, st.broker.port,
                             (st.broker.ws_path or self.cfg.ws_path) if st.broker.transport == "websockets" else "-",
                             self.topic("packets"), self.topic("status"))
                    await self._serve(st, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 (MqttError, LinkError, OSError, ...)
                st.last_error = str(exc) or type(exc).__name__
                log.warning("observer %s: %s", st.broker.name, st.last_error)
            finally:
                if st.connected:
                    log.info("observer: disconnected from %s", st.broker.host)
                st.connected = False
            if self._stop.is_set():
                break
            if not st.last_error:
                delay = 2.0   # a clean reconnect (token refresh): no backoff
                continue
            if self._clock() - started > 120:
                delay = 2.0
            wait = delay + self._rng.uniform(0, delay * 0.25)
            st.retry_at = self._clock() + wait
            await self._sleep(wait)
            st.retry_at = None
            delay = min(delay * 2, 300.0)

    async def _serve(self, st: BrokerState, client) -> None:
        await client.publish(self.topic("status"), self.status_message("online"), qos=0, retain=True)
        next_status = self._clock() + self.cfg.status_minutes * 60
        next_stats = self._clock() + 300
        exp = self.token_expiry(st.broker)
        while not self._stop.is_set():
            if exp is not None and exp - self._clock() <= 300:
                log.info("observer: token for %s expires; reconnecting with a new one", st.broker.host)
                return
            while st.queue:
                await client.publish(self.topic("packets"), st.queue[0], qos=0, retain=False)
                st.queue.popleft()
                st.sent += 1
                st.last_publish = self._clock()
            if self._clock() >= next_stats:
                ago = "never" if st.last_publish is None else f"{int(self._clock() - st.last_publish)} s ago"
                log.info("observer %s: sent %d, dropped %d, queued %d, last packet publish %s",
                         st.broker.name, st.sent, st.dropped, len(st.queue), ago)
                next_stats = self._clock() + 300
            if self._clock() >= next_status:
                await client.publish(self.topic("status"), self.status_message("online"), qos=0, retain=True)
                next_status = self._clock() + self.cfg.status_minutes * 60
            st.wake.clear()
            if st.queue:
                continue
            try:
                await asyncio.wait_for(st.wake.wait(), 5.0)
            except asyncio.TimeoutError:
                pass
        # clean stop: say so (the LWT only fires on an unclean disconnect)
        await client.publish(self.topic("status"), self.status_message("offline"), qos=0, retain=True)
