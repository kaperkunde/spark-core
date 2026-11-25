"""Choosing the Bluetooth adapter (Linux/BlueZ). Everything else ignores it.

The setting is an adapter name ("hci1") or the adapter's MAC address ("AA:BB:CC:DD:EE:FF"): hciN
numbers can change between reboots when there is more than one adapter (a USB dongle next to the
onboard chip), the MAC does not. Empty means the system default.

BlueZ lists adapters on D-Bus (/org/bluez/hciN with an Address property), so a MAC is resolved with
`busctl` (JSON), then `hciconfig`, then /sys/class/bluetooth/hciN/address if a kernel provides one.
`bluetoothctl list` shows MACs but not the hciN names, so it is only used for the error message.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("spark_bridge.ble")

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HCI_RE = re.compile(r"^hci\d+$")
SYSFS = Path("/sys/class/bluetooth")

_warned_ignored = False

ADAPTER_HELP = ("Bluetooth adapter on Linux/BlueZ: hciN (e.g. hci1) or the adapter's MAC address "
                "(stable across reboots); default: BRIDGE_BLE_ADAPTER, else the system default. "
                "Ignored on macOS.")


class AdapterError(Exception):
    pass


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_busctl(text: str) -> dict[str, str]:
    """`busctl --json=short call org.bluez / ...ObjectManager GetManagedObjects` -> {"AA:..": "hci1"}."""
    try:
        objs = json.loads(text)["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return {}
    out: dict[str, str] = {}
    for path, ifaces in (objs or {}).items():
        adapter = (ifaces or {}).get("org.bluez.Adapter1")
        if not adapter or not HCI_RE.match(path.rsplit("/", 1)[-1]):
            continue
        addr = adapter.get("Address", {}).get("data")
        if addr:
            out[addr.upper()] = path.rsplit("/", 1)[-1]
    return out


def parse_hciconfig(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur = ""
    for line in text.splitlines():
        m = re.match(r"^(hci\d+):", line)
        if m:
            cur = m.group(1)
        m = re.search(r"BD Address:\s*((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", line)
        if m and cur:
            out[m.group(1).upper()] = cur
    return out


def parse_bluetoothctl_list(text: str) -> list[str]:
    return [m.group(1).upper() for m in
            re.finditer(r"^Controller\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", text, re.M)]


def adapters_by_mac(sysfs: Path = SYSFS) -> dict[str, str]:
    """MAC (upper case) -> hciN for every adapter BlueZ knows."""
    found: dict[str, str] = {}
    found.update(parse_hciconfig(_run(["hciconfig", "-a"])))
    found.update(parse_busctl(_run(["busctl", "--system", "--json=short", "call", "org.bluez", "/",
                                    "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"])))
    try:
        for d in sysfs.glob("hci*"):
            f = d / "address"
            if f.is_file():
                found.setdefault(f.read_text().strip().upper(), d.name)
    except OSError:
        pass
    return found


def resolve_adapter(spec: str, *, table: dict[str, str] | None = None) -> str:
    """The BlueZ adapter name for a setting ("" = default, "hciN", or a MAC). Raises AdapterError."""
    spec = (spec or "").strip()
    if not spec:
        return ""
    if HCI_RE.match(spec.lower()):
        return spec.lower()
    if not MAC_RE.match(spec):
        raise AdapterError(f"ble_adapter {spec!r} is neither hciN nor a MAC address")
    table = adapters_by_mac() if table is None else table
    name = table.get(spec.upper())
    if name:
        return name
    known = ", ".join(f"{n} ({m})" for m, n in sorted(table.items(), key=lambda kv: kv[1]))
    if not known:
        ctl = parse_bluetoothctl_list(_run(["bluetoothctl", "list"]))
        known = ("controllers " + ", ".join(ctl)) if ctl else "none found"
    raise AdapterError(f"no Bluetooth adapter with address {spec} (adapters: {known}; "
                       "`bluetoothctl list` shows the controllers)")


def bleak_args(spec: str, *, platform: str | None = None, bleak_major: int | None = None) -> dict:
    """Keyword arguments for BleakScanner/BleakClient/BleakAdapter that select the adapter.

    bleak >= 3 takes bluez={"adapter": "hci1"}; bleak 0.x/1.x/2.x took adapter="hci1".
    Empty on macOS/Windows (the setting is ignored, once logged) and for the default adapter."""
    global _warned_ignored
    spec = (spec or "").strip()
    if not spec:
        return {}
    plat = platform or sys.platform
    if not plat.startswith("linux"):
        if not _warned_ignored:
            _warned_ignored = True
            log.warning("ble_adapter %r ignored: adapter selection only works on Linux/BlueZ", spec)
        return {}
    name = resolve_adapter(spec)
    if bleak_major is None:
        try:
            from importlib.metadata import version

            bleak_major = int(version("bleak").split(".")[0])
        except Exception:  # noqa: BLE001
            bleak_major = 3
    return {"bluez": {"adapter": name}} if bleak_major >= 3 else {"adapter": name}
