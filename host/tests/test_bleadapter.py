"""BLE adapter selection: pure parsing, bleak mocked (no radio, no BlueZ)."""
import asyncio
import json
import sys
import types

import pytest

from spark_bridge import bleadapter as ba
from spark_bridge import update
from spark_bridge.config import ConfigError, NodeConfig, check_adapter, from_env, parse
from spark_bridge.nodelink import LinkError, NodeLink

BUSCTL = json.dumps({"type": "a{oa{sa{sv}}}", "data": [{
    "/org/bluez": {"org.bluez.AgentManager1": {}},
    "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": {"type": "s", "data": "aa:00:00:00:00:01"}}},
    "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": {"type": "s", "data": "AA:00:00:00:00:02"}}},
    "/org/bluez/hci1/dev_11_22_33_44_55_66": {"org.bluez.Device1": {}},
}]})
HCICONFIG = ("hci1:\tType: Primary  Bus: USB\n\tBD Address: AA:00:00:00:00:02  ACL MTU: 1021:8\n"
             "hci0:\tType: Primary  Bus: UART\n\tBD Address: AA:00:00:00:00:01  ACL MTU: 1021:8\n")
TABLE = {"AA:00:00:00:00:01": "hci0", "AA:00:00:00:00:02": "hci1"}


def test_parsers():
    assert ba.parse_busctl(BUSCTL) == TABLE
    assert ba.parse_busctl("garbage") == {}
    assert ba.parse_hciconfig(HCICONFIG) == TABLE
    assert ba.parse_bluetoothctl_list("Controller aa:00:00:00:00:02 pi [default]\n") == ["AA:00:00:00:00:02"]


def test_resolve():
    assert ba.resolve_adapter("") == ""
    assert ba.resolve_adapter("hci1") == "hci1"
    assert ba.resolve_adapter("HCI1") == "hci1"
    assert ba.resolve_adapter("aa:00:00:00:00:02", table=TABLE) == "hci1"  # case-insensitive MAC
    with pytest.raises(ba.AdapterError, match="hci0"):
        ba.resolve_adapter("AA:00:00:00:00:99", table=TABLE)
    with pytest.raises(ba.AdapterError):
        ba.resolve_adapter("usb-dongle", table=TABLE)


def test_adapters_by_mac_uses_tools_and_sysfs(tmp_path, monkeypatch):
    (tmp_path / "hci2").mkdir()
    (tmp_path / "hci2" / "address").write_text("aa:00:00:00:00:03\n")

    def fake_run(cmd):
        return BUSCTL if cmd[0] == "busctl" else ""

    monkeypatch.setattr(ba, "_run", fake_run)
    assert ba.adapters_by_mac(tmp_path) == {**TABLE, "AA:00:00:00:00:03": "hci2"}


def test_bleak_args_by_platform_and_version(caplog):
    assert ba.bleak_args("") == {}
    assert ba.bleak_args("hci1", platform="linux", bleak_major=3) == {"bluez": {"adapter": "hci1"}}
    assert ba.bleak_args("hci1", platform="linux", bleak_major=0) == {"adapter": "hci1"}
    ba._warned_ignored = False
    with caplog.at_level("WARNING", logger="spark_bridge.ble"):
        assert ba.bleak_args("hci1", platform="darwin") == {}
        assert ba.bleak_args("hci1", platform="darwin") == {}
    assert sum("ignored" in r.message for r in caplog.records) == 1  # logged once


def test_config():
    assert NodeConfig().ble_adapter == ""
    assert from_env({"BRIDGE_BLE_ADAPTER": "hci1", "BRIDGE_BLE_NAME": "x"}).node.ble_adapter == "hci1"
    assert parse({"node": {"ble_adapter": " AA:00:00:00:00:02 "}}).node.ble_adapter == "AA:00:00:00:00:02"
    with pytest.raises(ConfigError):
        from_env({"BRIDGE_BLE_ADAPTER": "wlan0"})
    assert check_adapter("") == ""


def fake_bleak(monkeypatch):
    """A bleak whose scanner/client/adapter record the keyword arguments they get."""
    calls = {"scan": [], "client": [], "adapter": []}

    class Scanner:
        def __init__(self, detection_callback=None, **kw):
            calls["scan"].append(kw)
            self.cb = detection_callback

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        @classmethod
        async def find_device_by_filter(cls, flt, timeout=10.0, **kw):
            calls["scan"].append(kw)
            dev = types.SimpleNamespace(name="MeshCore-SparkNode", address="11:22:33:44:55:66")
            adv = types.SimpleNamespace(local_name="MeshCore-SparkNode")
            return dev if flt(dev, adv) else None

    class Client:
        def __init__(self, dev, **kw):
            calls["client"].append(kw)

    class Adapter:
        @classmethod
        async def get(cls, **kw):
            calls["adapter"].append(kw)
            return cls()

        async def get_connected_devices(self, uuids): return []

    mod = types.ModuleType("bleak")
    mod.BleakScanner, mod.BleakClient, mod.BleakAdapter = Scanner, Client, Adapter
    monkeypatch.setitem(sys.modules, "bleak", mod)
    return calls


async def test_nodelink_scans_on_chosen_adapter(monkeypatch):
    calls = fake_bleak(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    created = {}

    async def create_ble(**kw):
        created.update(kw)
        return None  # "no response": enough to stop after the create call

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", create_ble)
    link = NodeLink(NodeConfig(ble_name="SparkNode", ble_adapter="hci1"))
    with pytest.raises(LinkError):
        await link._create()
    assert calls["scan"] == [{"bluez": {"adapter": "hci1"}}]
    assert created["device"].address == "11:22:33:44:55:66"
    assert "address" not in created


async def test_nodelink_default_adapter_untouched(monkeypatch):
    calls = fake_bleak(monkeypatch)
    created = {}

    async def create_ble(**kw):
        created.update(kw)

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", create_ble)
    with pytest.raises(LinkError):
        await NodeLink(NodeConfig(ble_name="SparkNode"))._create()
    assert calls["scan"] == [] and created == {"address": "SparkNode", "default_timeout": 10.0,
                                               "auto_reconnect": False}


async def test_nodelink_ignores_adapter_on_macos(monkeypatch):
    calls = fake_bleak(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    ba._warned_ignored = False
    created = {}

    async def create_ble(**kw):
        created.update(kw)

    monkeypatch.setattr("spark_bridge.nodelink.MeshCore.create_ble", create_ble)
    with pytest.raises(LinkError):
        await NodeLink(NodeConfig(ble_name="SparkNode", ble_adapter="hci1"))._create()
    assert calls["scan"] == [] and created["address"] == "SparkNode"


async def test_nodelink_unknown_adapter_mac_is_a_link_error(monkeypatch):
    fake_bleak(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ba, "adapters_by_mac", lambda: TABLE)
    with pytest.raises(LinkError, match="no Bluetooth adapter"):
        await NodeLink(NodeConfig(ble_adapter="AA:00:00:00:00:99"))._create()


async def test_update_scan_and_bootloader_use_adapter(monkeypatch):
    calls = fake_bleak(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ba, "adapters_by_mac", lambda: TABLE)
    assert await update.scan(0, "AA:00:00:00:00:02") == []
    assert calls["scan"] == [{"bluez": {"adapter": "hci1"}}]
    from spark_bridge import dfu

    with pytest.raises(dfu.DfuError):  # nothing advertising in the fake; only the scanner kwargs matter
        await update.find_bootloader("", 0, "hci0")
    assert calls["scan"][-1] == {"bluez": {"adapter": "hci0"}}


async def test_dfu_transport_client_gets_adapter(monkeypatch):
    from spark_bridge import dfu

    calls = fake_bleak(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    t = dfu.BleakTransport("dev", adapter="hci1")
    with pytest.raises(Exception):  # the fake client cannot connect; only the constructor kwargs matter
        await t.connect()
    assert calls["client"][0]["bluez"] == {"adapter": "hci1"}


def test_cli_flags():
    from spark_bridge.__main__ import build_parser

    assert build_parser().parse_args(["--ble-adapter", "hci1"]).ble_adapter == "hci1"
    assert build_parser().parse_args([]).ble_adapter is None
    assert update.build_parser().parse_args(["x.zip", "--ble-adapter", "hci2"]).ble_adapter == "hci2"
