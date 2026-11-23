"""MQTT observer: packet format, node-signed token, publisher with a fake broker, stub integration."""
import asyncio
import base64
import hashlib
import json

import pytest

from spark_bridge.config import ConfigError, NodeConfig, from_env
from spark_bridge.nodelink import NodeLink, RxPacket
from spark_bridge.observer import (Broker, Observer, ObserverConfig, ObserverError, decode_packet,
                                   packet_message, parse_brokers, token_signing_input, verify_token)

from test_nodelink import wait_for

T0 = 1700000000


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- format --------------------------------------------------------------------------------

def test_decode_flood_packet_and_hash():
    payload = b"\x11\x22\x33abc"
    raw = bytes([0x15, 0x42]) + bytes.fromhex("a1b2c3d4") + payload    # GRP_TXT, flood, 2 hops x 2 bytes
    d = decode_packet(raw)
    assert (d["packet_type"], d["route"], d["payload_len"], d["path"]) == (5, "F", len(payload), bytes.fromhex("a1b2c3d4"))
    assert d["hash"] == hashlib.sha256(b"\x05" + payload).hexdigest()[:16].upper()


def test_decode_transport_direct_and_trace_hash():
    payload = b"\x01\x02\x03\x04"
    raw = bytes([0x27]) + b"\xaa\xbb\xcc\xdd" + bytes([0x02, 0x10, 0x20]) + payload   # TRACE (9), transport direct
    d = decode_packet(raw)
    assert (d["packet_type"], d["route"], d["payload_len"]) == (9, "T", 4)
    assert d["hash"] == hashlib.sha256(b"\x09" + (2).to_bytes(2, "little") + payload).hexdigest()[:16].upper()
    with pytest.raises(ValueError):
        decode_packet(b"\x15")


def test_packet_message_matches_meshcoretomqtt_shape():
    raw = bytes([0x0A, 0x02, 0x1A, 0x2B]) + b"xyz"   # TXT_MSG direct, 2 hops
    m = packet_message(raw, 5.25, -95, T0, "WoonSpArk", "AB" * 32)
    assert m == {"origin": "WoonSpArk", "origin_id": "AB" * 32, "timestamp": "2023-11-14T22:13:20+00:00",
                 "type": "PACKET", "direction": "rx", "time": "22:13:20", "date": "14/11/2023", "len": "7",
                 "packet_type": "2", "route": "D", "payload_len": "3", "raw": raw.hex().upper(),
                 "SNR": "5.25", "RSSI": "-95", "hash": decode_packet(raw)["hash"], "path": "1A,2B"}
    bad = packet_message(b"\x01", 0.0, -100, T0, "x", "y")
    assert bad["route"] == "U" and bad["hash"] == "0" * 16


def test_brokers_presets_and_urls():
    names = [b.host for b in parse_brokers(ObserverConfig(brokers=["dutchmeshcore"]))]
    assert names == ["collector1.dutchmeshcore.nl", "collector2.dutchmeshcore.nl"]
    b = parse_brokers(ObserverConfig(brokers=["mqtts://mqtt.example.nl:8883"], username="u"))[0]
    assert (b.host, b.port, b.transport, b.tls, b.auth) == ("mqtt.example.nl", 8883, "tcp", True, "password")
    w = parse_brokers(ObserverConfig(brokers=["wss://x.nl/mqtt"]))[0]
    assert (w.port, w.transport, w.ws_path, w.auth) == (443, "websockets", "/mqtt", "token")
    with pytest.raises(ObserverError):
        parse_brokers(ObserverConfig(brokers=["ftp://x"]))


def test_env_config_and_password_by_name(tmp_path):
    env = {"BRIDGE_OBSERVER": "1", "BRIDGE_OBSERVER_IATA": "ams", "BRIDGE_OBSERVER_BROKERS": "letsmesh-eu, mqtt://h:1883",
           "BRIDGE_OBSERVER_USERNAME": "obs", "BRIDGE_OBSERVER_PASSWORD_ENV": "MY_MQTT_PW", "MY_MQTT_PW": "s3cret"}
    cfg = from_env(env, tmp_path)
    assert cfg.observer.enabled and cfg.observer.brokers == ["letsmesh-eu", "mqtt://h:1883"]
    assert cfg.observer_password == "s3cret"
    with pytest.raises(ConfigError):
        from_env({"BRIDGE_OBSERVER": "1"}, tmp_path)   # no region code


# ---- token ---------------------------------------------------------------------------------

class SignNode:
    """A node with a real Ed25519 identity (stand-in for NodeLink)."""

    def __init__(self):
        from Crypto.PublicKey import ECC
        self.key = ECC.generate(curve="ed25519")
        self.pubkey = self.key.public_key().export_key(format="raw").hex()
        self.node_name, self.version, self.connected, self.features, self.radio = "WoonSpArk", "v1.18.0-SparkNode1.3", True, 1, "869.618,62.5,7,5"
        self.signed: list[bytes] = []

    async def sign(self, data: bytes) -> bytes:
        from Crypto.Signature import eddsa
        self.signed.append(data)
        return eddsa.new(self.key, "rfc8032").sign(data)


async def test_token_is_signed_by_the_node():
    node = SignNode()
    obs = Observer(ObserverConfig(enabled=True, iata="AMS", owner="cd" * 32), node, clock=lambda: T0)
    user, token = await obs.credentials(Broker("d1", "collector1.dutchmeshcore.nl"))
    assert user == "v1_" + node.pubkey.upper()
    h, p, sig = token.split(".")
    assert json.loads(b64d(h)) == {"alg": "Ed25519", "typ": "JWT"}
    claims = json.loads(b64d(p))
    assert claims["publicKey"] == node.pubkey.upper() and claims["aud"] == "collector1.dutchmeshcore.nl"
    assert claims["exp"] - claims["iat"] == 3600 and claims["owner"] == "CD" * 32 and "email" not in claims
    assert sig == sig.upper() and len(sig) == 128
    assert verify_token(token, node.pubkey) and node.signed == [f"{h}.{p}".encode()]
    # cached until 5 minutes before expiry
    assert (await obs.credentials(Broker("d1", "collector1.dutchmeshcore.nl")))[1] == token
    assert len(node.signed) == 1
    assert not verify_token(token, "00" * 32)


def test_signing_input_shape():
    si = token_signing_input("ab" * 32, "h", 1, 60)
    assert si.count(".") == 1 and "=" not in si


# ---- publisher with a fake broker ------------------------------------------------------------

class FakeBroker:
    def __init__(self, fail_first: int = 0):
        self.fail_first = fail_first
        self.connects: list[dict] = []
        self.published: list[tuple[str, str, bool]] = []
        self.open = False

    def factory(self, **kw):
        broker = self

        class Client:
            async def __aenter__(self):
                broker.connects.append(kw)
                if len(broker.connects) <= broker.fail_first:
                    raise OSError("connection refused")
                broker.open = True
                return self

            async def __aexit__(self, *a):
                broker.open = False

            async def publish(self, topic, payload, qos=0, retain=False):
                broker.published.append((topic, payload, retain))

        return Client()


def rx(raw=bytes([0x15, 0x00]) + b"abc", snr=5.0, rssi=-90):
    return RxPacket(T0, snr, rssi, raw)


async def test_publishes_status_packets_and_offline():
    node, fb = SignNode(), FakeBroker()
    obs = Observer(ObserverConfig(enabled=True, iata="ams", brokers=["dutchmeshcore-1"]), node,
                   client_factory=fb.factory)
    task = asyncio.create_task(obs.run())
    try:
        await wait_for(lambda: fb.open)
        kw = fb.connects[0]
        assert kw["hostname"] == "collector1.dutchmeshcore.nl" and kw["port"] == 443
        assert kw["transport"] == "websockets" and kw["websocket_path"] == "/" and kw["tls_context"] is not None
        assert kw["username"] == "v1_" + node.pubkey.upper() and kw["identifier"].startswith("meshcore_")
        will_topic, will = kw["will"]
        assert will_topic == f"meshcore/AMS/{node.pubkey.upper()}/status" and json.loads(will)["status"] == "offline"
        obs.on_rx(rx())
        await wait_for(lambda: any(t.endswith("/packets") for t, _, _ in fb.published))
        status = [json.loads(p) for t, p, r in fb.published if t.endswith("/status") and r]
        assert status[0]["status"] == "online" and status[0]["radio"] == "869.618,62.5,7,5"
        pkt = [json.loads(p) for t, p, r in fb.published if t.endswith("/packets") and not r][0]
        assert pkt["origin"] == "WoonSpArk" and pkt["raw"] == (bytes([0x15, 0]) + b"abc").hex().upper()
        assert "sent 1, dropped 0" in obs.summary() and "connected" in obs.summary()
    finally:
        obs.stop()
        await asyncio.wait_for(task, 5)
    assert json.loads(fb.published[-1][1])["status"] == "offline" and fb.published[-1][2]


async def test_backoff_then_connects_and_bounded_queue():
    node, fb = SignNode(), FakeBroker(fail_first=2)
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)

    obs = Observer(ObserverConfig(enabled=True, iata="AMS", brokers=["letsmesh-eu"], queue_size=3), node,
                   client_factory=fb.factory, sleep=fake_sleep)
    for _ in range(5):
        obs.on_rx(rx())       # queued while not connected; the oldest two are dropped
    assert obs.brokers[0].dropped == 2
    task = asyncio.create_task(obs.run())
    try:
        await wait_for(lambda: obs.brokers[0].sent == 3)
        assert len(fb.connects) == 3 and sleeps[0] >= 2 and sleeps[1] >= 4
    finally:
        obs.stop()
        await asyncio.wait_for(task, 5)


async def test_waits_for_the_node_before_connecting():
    node, fb = SignNode(), FakeBroker()
    node.connected = False
    obs = Observer(ObserverConfig(enabled=True, iata="AMS", brokers=["letsmesh-eu"]), node, client_factory=fb.factory)
    task = asyncio.create_task(obs.run())
    await asyncio.sleep(0.2)
    assert fb.connects == [] and "link down" in obs.summary()
    node.connected = True
    await wait_for(lambda: fb.open, 3)
    obs.stop()
    await asyncio.wait_for(task, 5)


def test_password_auth_needs_the_secret():
    with pytest.raises(ObserverError):
        Observer(ObserverConfig(enabled=True, iata="AMS", brokers=["mqtts://h"], username="u"), SignNode())


# ---- integration: real NodeLink, stub node, fake broker -------------------------------------

async def test_stub_feed_and_node_signed_token(stub):
    stub.use_sign_key()
    fb = FakeBroker()
    link = NodeLink(NodeConfig(transport="tcp", tcp_port=stub.port, command_timeout=3, backoff_start=0.05,
                               backoff_max=0.2), clock=lambda: T0)
    obs = Observer(ObserverConfig(enabled=True, iata="AMS", brokers=["dutchmeshcore-2"]), link, client_factory=fb.factory)
    link.rx_log = obs.on_rx
    tasks = [asyncio.create_task(link.run()), asyncio.create_task(obs.run())]
    try:
        await wait_for(lambda: fb.open)
        assert link.features == 3 and stub.features == 3
        token = fb.connects[0]["password"]
        assert verify_token(token, stub.sign_pubkey.hex())
        raw = bytes([0x11, 0x41, 0xa1, 0xb2]) + b"advert-ish"
        assert stub.inject_rx(raw, snr=-2.5, rssi=-110)
        await wait_for(lambda: any(t.endswith("/packets") for t, _, _ in fb.published))
        pkt = json.loads([p for t, p, _ in fb.published if t.endswith("/packets")][0])
        assert pkt["raw"] == raw.hex().upper() and pkt["SNR"] == "-2.5" and pkt["RSSI"] == "-110"
        assert pkt["packet_type"] == "4" and pkt["origin_id"] == stub.sign_pubkey.hex().upper()
        # the node refuses to sign anything but a JWT signing input
        from spark_bridge.nodelink import LinkError
        with pytest.raises(LinkError):
            await link.sign(b"\x00" * 40)
    finally:
        obs.stop()
        link.stop()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
