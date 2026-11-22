"""`spark-bridge update <zip>`: update a SparkNode over Bluetooth.

1. Check the package offline (dfu.load_package).
2. Take the link lock; a running bridge daemon is asked to pause (SIGUSR1) and hands it over.
3. Preflight over the normal link (BLE, or USB serial): firmware with `dfu` (SparkNode2+),
   running build, battery.
4. Send `dfu`: the node answers with the bootloader's address (its own + 1), closes the link and
   resets into the bootloader, unbonded. The bridge's own BLE client is disconnected explicitly.
5. Find the bootloader by its live advertisement (DFU service, no NUS), never by a cached name.
6. Transfer (dfu.transfer: receipts checked, retry policy by bootloader type).
7. Verify: reconnect to the app and compare `get build` with the package's build id.
8. Release the lock: the daemon reconnects by itself.

Fallbacks: on Linux the temporary bootloader device (MAC+1) is removed from BlueZ afterwards
(`bluetoothctl remove`, never the bonded app device); on macOS, if the app cannot be reached
after the update, the Bluetooth radio is toggled once with blueutil when it is installed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from . import dfu
from .config import ConfigError, load_env
from .lock import LinkLock, lock_path_for
from .nodelink import LinkError, NodeLink

log = logging.getLogger("spark_bridge.update")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_NO_APP = 0, 1, 2, 4
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
BUILD_RE = re.compile(r">\s*(v\S+)\s+(\S+)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="spark-bridge update",
        description="Update a SparkNode (SparkNode2 or later) over Bluetooth with a DFU package "
                    "(pio run -e SparkNode_P1 -t dfu_zip -> .pio/build/SparkNode_P1/firmware-dfu.zip).")
    ap.add_argument("zip", help="application DFU package (.zip)")
    ap.add_argument("--env", "-e", default=None, help="the .env file (default as for the bridge)")
    ap.add_argument("--force", action="store_true",
                    help="update even with a low battery (sends 'dfu force'); also allows the same build")
    ap.add_argument("--min-mv", type=int, default=3700, help="refuse below this battery voltage (default 3700)")
    ap.add_argument("--in-bootloader", action="store_true",
                    help="the node already waits in the bootloader (an earlier attempt broke off): skip "
                         "the app steps and transfer to the AdaDFU device that advertises")
    ap.add_argument("--bootloader-address", default="",
                    help="with --in-bootloader: only use this address (Linux MAC, or macOS with use_bdaddr)")
    ap.add_argument("--attempts", type=int, default=3, help="transfer attempts (default 3)")
    ap.add_argument("--scan-seconds", type=float, default=30.0, help="how long to look for the bootloader")
    ap.add_argument("--verify-seconds", type=float, default=120.0, help="how long to wait for the new app")
    ap.add_argument("--no-verify", action="store_true", help="do not reconnect to check the build id")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--lock-timeout", type=float, default=60.0,
                    help="how long to wait for a running bridge to hand over the link")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap


def say(msg: str) -> None:
    print(msg, flush=True)


def parse_build(reply: str) -> str | None:
    m = BUILD_RE.search(reply or "")
    return f"{m.group(1)} {m.group(2)}" if m else None


# --- link lock ----------------------------------------------------------------------------------------

async def take_lock(lock: LinkLock, timeout: float) -> bool:
    if lock.try_acquire():
        return True
    pid = lock.holder_pid()
    if pid and pid != os.getpid():
        say(f"The bridge (pid {pid}) holds the link; asking it to pause.")
        try:
            os.kill(pid, signal.SIGUSR1)
        except ProcessLookupError:
            pass
        except PermissionError:
            say("Not allowed to signal it (another user?).")
    return await lock.acquire(timeout=timeout)


def lock_help() -> str:
    if sys.platform == "darwin":
        return ("launchctl unload ~/Library/LaunchAgents/com.kaperkunde.spark-bridge.plist  "
                "(and 'launchctl load ...' afterwards)")
    return "sudo systemctl stop spark-bridge  (and 'sudo systemctl start spark-bridge' afterwards)"


# --- bootloader discovery -------------------------------------------------------------------------------

async def find_bootloader(address: str, timeout: float):
    """The BLEDevice of a live DFU-bootloader advertisement. With a MAC address (Linux, or macOS
    when bleak can report real addresses) only that device; otherwise exactly one advertiser."""
    from bleak import BleakScanner

    kw = {}
    if sys.platform == "darwin":
        kw["cb"] = {"use_bdaddr": True}
    found: dict[str, object] = {}

    def on_adv(device, adv):
        if dfu.is_bootloader_adv(adv.service_uuids, adv.local_name or device.name):
            found[device.address.upper()] = device

    want = address.upper() if address and MAC_RE.match(address) else ""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    async with BleakScanner(detection_callback=on_adv, **kw):
        first_seen = None
        while loop.time() < end:
            await asyncio.sleep(0.5)
            if want and want in found:
                return found[want]
            if not want and found:
                first_seen = first_seen or loop.time()
                if loop.time() - first_seen >= 3.0:  # give a second advertiser time to show up
                    break
    if want:
        others = ", ".join(found) or "none"
        raise dfu.DfuError(f"bootloader {want} not seen within {timeout:.0f} s (DFU advertisers: {others})")
    macs = [a for a in found if MAC_RE.match(a)]
    if not found:
        raise dfu.DfuError(f"no DFU bootloader advertising within {timeout:.0f} s")
    if len(found) > 1:
        raise dfu.DfuError(f"{len(found)} DFU bootloaders advertise ({', '.join(found)}); "
                           "pass --bootloader-address")
    if address and not macs:
        log.info("this platform hides addresses; using the only DFU advertiser")
    return next(iter(found.values()))


# --- OS fallbacks ---------------------------------------------------------------------------------------

def bluez_remove(mac: str, app_mac: str) -> None:
    """Forget the temporary bootloader device in BlueZ. Never the bonded app device."""
    if not sys.platform.startswith("linux") or not shutil.which("bluetoothctl"):
        return
    if not MAC_RE.match(mac or "") or (app_mac and mac.upper() == app_mac.upper()):
        log.warning("not removing %s from BlueZ (not a distinct bootloader address)", mac)
        return
    subprocess.run(["bluetoothctl", "remove", mac], capture_output=True, timeout=15)
    log.info("removed the bootloader device %s from BlueZ", mac)


def mac_radio_toggle() -> bool:
    if sys.platform != "darwin" or not shutil.which("blueutil"):
        return False
    say("Toggling the Mac's Bluetooth radio (blueutil) to drop stale GATT state.")
    subprocess.run(["blueutil", "--power", "0"], timeout=15)
    time.sleep(3)
    subprocess.run(["blueutil", "--power", "1"], timeout=15)
    time.sleep(5)
    return True


def keep_awake():
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    return None


# --- steps ------------------------------------------------------------------------------------------

async def node_session(cfg_node):
    link = NodeLink(cfg_node)
    await link.connect()
    return link


async def read_build(link: NodeLink) -> str | None:
    return parse_build(await link.cli("get build"))


async def verify(cfg_node, pkg: dfu.Package, seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    toggled = False
    last = ""
    while loop.time() < end:
        try:
            link = await node_session(cfg_node)
            try:
                running = await read_build(link)
            finally:
                await link.close()
            if running == pkg.build:
                say(f"Verified: the node runs {running}.")
                return True
            say(f"The node runs {running!r}, expected {pkg.build!r}.")
            return False
        except (LinkError, OSError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            last = str(exc)
            log.info("app not reachable yet: %s", exc)
            if not toggled and re.search(r"not found|ATT|GATT|characteristic|not permitted", last, re.I):
                toggled = mac_radio_toggle()
            await asyncio.sleep(5)
    say(f"Could not reach the app within {seconds:.0f} s ({last}).")
    if sys.platform.startswith("linux"):
        say("On BlueZ a stale GATT cache can be removed with: sudo rm /var/lib/bluetooth/<adapter>/cache/<node MAC>"
            " then sudo systemctl restart bluetooth. Do NOT 'bluetoothctl remove' the node: that drops the bond.")
    elif sys.platform == "darwin" and not toggled:
        say("Toggle Bluetooth off and on once (or install blueutil: brew install blueutil) and run "
            "'spark-bridge update --help' / check with the bridge.")
    return False


async def run(args) -> int:
    try:
        cfg = load_env(args.env)
    except ConfigError as exc:
        say(f"config error: {exc}")
        return EXIT_CONFIG
    try:
        pkg = dfu.load_package(args.zip)
    except dfu.PackageError as exc:
        say(f"package error: {exc}")
        return EXIT_CONFIG
    say(f"Package: {args.zip}: {len(pkg.image)} bytes, build {pkg.build or '(unknown)'}")
    for w in pkg.warnings:
        say(f"warning: {w}")

    lock = LinkLock(lock_path_for(cfg.db_path))
    if not await take_lock(lock, args.lock_timeout):
        say(f"Could not get the link lock {lock.path}. Stop the bridge service and try again: {lock_help()}")
        return EXIT_FAILED
    caff = keep_awake()
    try:
        return await _run_locked(args, cfg, pkg)
    finally:
        lock.release()
        if caff is not None:
            caff.terminate()
        say("Link lock released (a running bridge reconnects by itself).")


async def _run_locked(args, cfg, pkg: dfu.Package) -> int:
    bl_mac = args.bootloader_address
    app_mac = cfg.node.ble_address if MAC_RE.match(cfg.node.ble_address or "") else ""
    if not args.in_bootloader:
        try:
            link = await node_session(cfg.node)
        except Exception as exc:  # noqa: BLE001
            say(f"Cannot reach the node: {exc}")
            return EXIT_FAILED
        try:
            running = await read_build(link)
            if running is None:
                say(f"The node ({link.version}) has no 'get build'/'dfu' (SparkNode1 or older). "
                    "Update it once over USB (setup.sh), then use this command.")
                return EXIT_FAILED
            mv = await link.battery()
            addr = getattr(link._bleak, "address", "") if link._bleak is not None else ""
            if MAC_RE.match(addr or ""):
                app_mac = addr.upper()
            say(f"Node: {link.node_name}, running {running}, battery {mv} mV")
            if running == pkg.build and not args.force:
                say("The node already runs this build (use --force to flash it anyway).")
                return EXIT_FAILED
            if mv < args.min_mv and not args.force:
                say(f"Battery {mv} mV is below {args.min_mv} mV: a brownout during the transfer leaves "
                    "the node without an app. Wait for sun, or use --force.")
                return EXIT_FAILED
            if not args.yes:
                ans = input(f"Update {link.node_name} to {pkg.build or args.zip}? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    say("Cancelled.")
                    return EXIT_FAILED
            reply = await link.cli("dfu force" if args.force else "dfu")
            bl_mac = dfu.parse_dfu_reply(reply)
            say(f"Node: {reply.strip()}")
        except dfu.DfuError as exc:
            say(str(exc))
            return EXIT_FAILED
        except LinkError as exc:
            say(f"Link error during preflight: {exc}")
            return EXIT_FAILED
        finally:
            await link.close()  # also disconnects the bleak client: the node must not stay connected

    async def connect():
        dev = await find_bootloader(bl_mac, args.scan_seconds)
        t = dfu.BleakTransport(dev)
        await t.connect()
        return t

    shown = [-1]

    def progress(done: int, total: int) -> None:
        tenth = done * 10 // total
        if tenth != shown[0]:
            shown[0] = tenth
            say(f"  {done * 100 // total:3d}%  {done}/{total} bytes")

    say(f"Looking for the bootloader{' ' + bl_mac if bl_mac else ''} ...")
    t0 = time.monotonic()
    try:
        rev = await dfu.transfer(connect, pkg, attempts=args.attempts, progress=progress)
    except dfu.DfuError as exc:
        say(f"Update failed: {exc}")
        if exc.app_erased:
            say("The node has NO application now. It is in its bootloader.")
            say("- Spark bootloader (0.11.0-spark1): it keeps waiting for BLE OTA. Retry with "
                f"'spark-bridge update --in-bootloader {args.zip}', or with a phone (nRF Connect / nRF DFU app).")
            say("- Stock bootloader: do not reset or power-cycle it if you can avoid it; recovery is USB "
                "(double reset, copy the UF2). See spark/README.md, 'Recovery'.")
            return EXIT_NO_APP
        say("The old application is intact (Reset sent). If the node does not come back, press reset once.")
        return EXIT_FAILED
    say(f"Transfer done in {time.monotonic() - t0:.0f} s (bootloader {rev or 'unknown'}).")
    bluez_remove(bl_mac, app_mac)
    if args.no_verify or not pkg.build_id:
        say("Not verified (no build id in the package, or --no-verify).")
        return EXIT_OK
    await asyncio.sleep(5)  # the app boots; BLE starts 30 s after boot
    return EXIT_OK if await verify(cfg.node, pkg, args.verify_seconds) else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        say("Interrupted. If the transfer had started, the node has no application and waits in its "
            "bootloader: run again with --in-bootloader (Spark bootloader), see spark/README.md 'Recovery'.")
        return EXIT_FAILED
