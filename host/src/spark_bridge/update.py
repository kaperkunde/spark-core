"""`spark-bridge update <zip>`: update a SpArkNode over Bluetooth.

1. Check the package offline (dfu.load_package).
2. Take the link lock; a running bridge daemon is asked to pause (SIGUSR1) and hands it over.
3. Preflight over the normal link (BLE, or USB serial): firmware with `dfu` (SpArkNode1.1+),
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
import collections
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

from . import dfu
from .bleadapter import ADAPTER_HELP, AdapterError, bleak_args, resolve_adapter
from .config import ConfigError, check_adapter, load_env
from .lock import LinkLock, lock_path_for
from .nodelink import LinkError, NodeLink

log = logging.getLogger("spark_bridge.update")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_NO_APP = 0, 1, 2, 4
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
BUILD_RE = re.compile(r">\s*(v\S+)\s+(\S+)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="spark-bridge update",
        description="Update a SpArkNode (SpArkNode1.1 or later) over Bluetooth with a DFU package "
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
    ap.add_argument("--ble-adapter", default=None, metavar="hciN|MAC", help=ADAPTER_HELP)
    ap.add_argument("--attempts", type=int, default=3, help="transfer attempts (default 3)")
    ap.add_argument("--prn", type=int, default=dfu.PRN, choices=range(1, dfu.MAX_PRN + 1), metavar="N",
                    help=f"packets per receipt (1..{dfu.MAX_PRN}, default {dfu.PRN}; the bootloader runs out "
                         "of buffers above 8, so lower it only for a bad link)")
    ap.add_argument("--packet-size", type=int, default=0, metavar="BYTES",
                    help="bytes per data packet: 0 (default) = as large as the negotiated ATT MTU allows, up to "
                         f"{dfu.MAX_PACKET_SIZE}; 20 = legacy packets. A retry after a failure always uses 20")
    ap.add_argument("--scan-seconds", type=float, default=30.0, help="how long to look for the bootloader")
    ap.add_argument("--verify-seconds", type=float, default=120.0, help="how long to wait for the new app")
    ap.add_argument("--no-verify", action="store_true", help="do not reconnect to check the build id")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--lock-timeout", type=float, default=60.0,
                    help="how long to wait for a running bridge to hand over the link")
    ap.add_argument("--status-file", default="", metavar="PATH",
                    help="keep a small JSON status file (phase, pct, rate, ETA, state) up to date, "
                         "for scripts and agents that poll instead of reading the log")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)  # one line each, no \r: fine in a log file


# --- progress reporting ---------------------------------------------------------------------------------

STEPS = 5  # connect, dfu command, bootloader scan, transfer, verify
PRINT_PCT, PRINT_SECONDS, RATE_WINDOW = 5, 15.0, 45.0
TYPICAL_RATE = 1300.0  # bytes/s seen on the roof node (391 KB took about 5 minutes)


def fmt_duration(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "--:--"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def fmt_progress(done: int, total: int, rate: float | None, elapsed: float) -> str:
    """'transfer 45%  176k/391k bytes  1.31 kB/s  elapsed 2:14  ETA 2:44'"""
    eta = (total - done) / rate if rate and rate > 0 else None
    rate_s = f"{rate / 1000:.2f} kB/s" if rate else "-- kB/s"
    return (f"transfer {done * 100 // max(total, 1)}%  {done // 1000}k/{total // 1000}k bytes  {rate_s}  "
            f"elapsed {fmt_duration(elapsed)}  ETA {fmt_duration(eta)}")


class Reporter:
    """Phase lines, throttled transfer progress (every 5 % or 15 s), end summary, optional status file."""

    def __init__(self, status_file: str = "", resume_base: str = "", clock=time.monotonic):
        self.status_file, self.resume_base, self.clock = status_file, resume_base, clock
        self.t_start = clock()
        self.phase, self.step = "start", 0
        self.bl_mac = ""
        self.app_erased = False
        self.build = ""
        self._reset_transfer()
        self.last = {}

    def _reset_transfer(self) -> None:
        self.samples: collections.deque = collections.deque()
        self.t_transfer = self.clock()
        self.next_pct, self.last_print = PRINT_PCT, self.clock()

    def start_phase(self, step: int, phase: str, text: str) -> None:
        self.step, self.phase = step, phase
        say(f"[{step}/{STEPS}] {text}")
        self._status("running", pct=0 if phase == "transfer" else None)

    def rate(self) -> float | None:
        """Bytes/s over the last RATE_WINDOW seconds (needs >= 2 s of data), else the overall average."""
        if len(self.samples) < 2:
            return None
        (t0, d0), (t1, d1) = self.samples[0], self.samples[-1]
        if t1 - t0 >= 2.0:
            return (d1 - d0) / (t1 - t0)
        return d1 / (t1 - self.t_transfer) if t1 - self.t_transfer >= 2.0 else None

    def progress(self, done: int, total: int) -> None:
        now = self.clock()
        if self.samples and done < self.samples[-1][1]:  # a retry starts over
            self._reset_transfer()
        if not self.samples:
            self.samples.append((self.t_transfer, 0))
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[1][0] > RATE_WINDOW:
            self.samples.popleft()
        pct = done * 100 // max(total, 1)
        if pct >= self.next_pct or now - self.last_print >= PRINT_SECONDS:
            self.next_pct = (pct // PRINT_PCT + 1) * PRINT_PCT
            self.last_print = now
            rate = self.rate()
            say(fmt_progress(done, total, rate, now - self.t_transfer))
            eta = (total - done) / rate if rate else None
            self._status("running", pct=pct, bytes=done, total=total,
                         rate=round(rate, 1) if rate else None, eta_s=round(eta) if eta is not None else None)

    def resume_hint(self) -> str:
        if self.phase == "verify":
            return (f"the transfer finished; check what the node runs (spark-bridge scan, the bridge log), "
                    f"and only if it still runs the old build: {self.resume_base} --force")
        if self.app_erased or self.phase == "bootloader-scan":  # the node waits in its bootloader
            addr = f" --bootloader-address {self.bl_mac}" if MAC_RE.match(self.bl_mac or "") else ""
            return f"{self.resume_base}{'' if '--in-bootloader' in self.resume_base else ' --in-bootloader'}{addr}"
        return self.resume_base

    def finish(self, code: int) -> None:
        total = self.clock() - self.t_start
        if code == EXIT_OK:
            rate = self.last.get("avg_rate")
            avg = f", average {rate / 1000:.2f} kB/s" if rate else ""
            say(f"Done: update finished in {fmt_duration(total)}{avg}, "
                f"{'verified build ' + self.build if self.build else 'build not verified'}.")
            self._status("done", pct=100)
        else:
            hint = self.resume_hint()
            say(f"FAILED in phase '{self.phase}' [{self.step}/{STEPS}] after {fmt_duration(total)}. "
                f"To resume: {hint}")
            self._status("failed")

    def _status(self, state: str, **kw) -> None:
        if not self.status_file:
            return
        self.last.update({k: v for k, v in kw.items() if v is not None})
        doc = {"phase": self.phase, "step": self.step, "steps": STEPS, "pct": None, "bytes": None,
               "total": None, "rate": None, "eta_s": None, "state": state,
               "resume_hint": self.resume_hint() if state == "failed" else "",
               "build": self.build, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        doc.update({k: v for k, v in self.last.items() if k in doc})
        if state == "done":
            doc["eta_s"] = 0
        try:
            tmp = f"{self.status_file}.tmp"
            with open(tmp, "w") as f:
                json.dump(doc, f)
            os.replace(tmp, self.status_file)  # atomic: a poller never sees half a file
        except OSError as exc:
            log.warning("cannot write the status file: %s", exc)


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

async def find_bootloader(address: str, timeout: float, adapter: str = ""):
    """The BLEDevice of a live DFU-bootloader advertisement. With a MAC address (Linux, or macOS
    when bleak can report real addresses) only that device; otherwise exactly one advertiser."""
    from bleak import BleakScanner

    kw = dict(bleak_args(adapter))
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


async def verify(cfg_node, pkg: dfu.Package, seconds: float, rp: Reporter | None = None) -> bool:
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
                if rp is not None:
                    rp.build = running
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
    parts = ["spark-bridge", "update", args.zip] + (["--env", args.env] if args.env else []) + ["--yes"]
    if args.in_bootloader:
        parts.append("--in-bootloader")
    rp = Reporter(args.status_file, " ".join(shlex.quote(p) for p in parts))
    try:
        code = await _run(args, rp)
    except BaseException:
        rp.finish(EXIT_FAILED)
        raise
    rp.finish(code)
    return code


async def _run(args, rp: Reporter) -> int:
    try:
        cfg = load_env(args.env)
        if args.ble_adapter is not None:
            cfg.node.ble_adapter = check_adapter(args.ble_adapter)
    except ConfigError as exc:
        say(f"config error: {exc}")
        return EXIT_CONFIG
    try:
        resolve_adapter(cfg.node.ble_adapter)  # fail early on an unknown adapter MAC
    except AdapterError as exc:
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
        return await _run_locked(args, cfg, pkg, rp)
    finally:
        lock.release()
        if caff is not None:
            caff.terminate()
        say("Link lock released (a running bridge reconnects by itself).")


async def _run_locked(args, cfg, pkg: dfu.Package, rp: Reporter) -> int:
    bl_mac = args.bootloader_address
    rp.bl_mac = bl_mac
    est = fmt_duration(len(pkg.image) / TYPICAL_RATE)
    app_mac = cfg.node.ble_address if MAC_RE.match(cfg.node.ble_address or "") else ""
    if args.in_bootloader:
        say(f"[1/{STEPS}] and [2/{STEPS}] skipped (--in-bootloader: the node already waits in its bootloader)")
    else:
        rp.start_phase(1, "connect", "connecting to the node over Bluetooth/USB, reading build and battery "
                                     "(normally 5-20 s)")
        try:
            link = await node_session(cfg.node)
        except Exception as exc:  # noqa: BLE001
            say(f"Cannot reach the node: {exc}")
            return EXIT_FAILED
        try:
            running = await read_build(link)
            if running is None:
                say(f"The node ({link.version}) has no 'get build'/'dfu' (SpArkNode1 or older). "
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
            rp.start_phase(2, "dfu-command", "sending 'dfu': the node closes the link and resets into its "
                                              "bootloader (normally 2-10 s)")
            reply = await link.cli("dfu force" if args.force else "dfu")
            bl_mac = dfu.parse_dfu_reply(reply)
            rp.bl_mac = bl_mac
            say(f"Node: {reply.strip()}")
        except dfu.DfuError as exc:
            say(str(exc))
            return EXIT_FAILED
        except LinkError as exc:
            say(f"Link error during preflight: {exc}")
            return EXIT_FAILED
        finally:
            await link.close()  # also disconnects the bleak client: the node must not stay connected

    attempts = [0]

    async def connect():
        attempts[0] += 1
        rp.start_phase(3, "bootloader-scan", f"waiting for bootloader {bl_mac or '(any DFU advertiser)'}"
                                             f" (attempt {attempts[0]}; normally 5-30 s, up to {args.scan_seconds:.0f} s)")
        dev = await find_bootloader(bl_mac, args.scan_seconds, cfg.node.ble_adapter)
        t = dfu.BleakTransport(dev, adapter=cfg.node.ble_adapter)
        await t.connect()
        rp.start_phase(4, "transfer", f"sending {len(pkg.image)} bytes; the old app is erased at the start. "
                                      f"Typically ~{TYPICAL_RATE / 1000:.1f} kB/s, so about {est} "
                                      f"(391 KB took ~5 min); lines every {PRINT_PCT}% or {PRINT_SECONDS:.0f} s")
        rp._reset_transfer()
        return t

    t0 = time.monotonic()
    try:
        rev = await dfu.transfer(connect, pkg, attempts=args.attempts, prn=args.prn,
                                   packet_size=args.packet_size, progress=rp.progress)
    except dfu.DfuError as exc:
        rp.app_erased = exc.app_erased
        say(f"Update failed: {exc}")
        if exc.app_erased:
            say("The node has NO application now. It is in its bootloader.")
            say("- SpArk bootloader (0.11.0-spark1): it keeps waiting for BLE OTA. Retry with "
                f"'spark-bridge update --in-bootloader {args.zip}', or with a phone (nRF Connect / nRF DFU app).")
            say("- Stock bootloader: do not reset or power-cycle it if you can avoid it; recovery is USB "
                "(double reset, copy the UF2). See spark/README.md, 'Recovery'.")
            return EXIT_NO_APP
        say("The old application is intact (Reset sent). If the node does not come back, press reset once.")
        return EXIT_FAILED
    took = time.monotonic() - t0
    rp.last["avg_rate"] = len(pkg.image) / took if took > 0 else None
    say(f"Transfer done in {took:.0f} s (bootloader {rev or 'unknown'}).")
    bluez_remove(bl_mac, app_mac)
    if args.no_verify or not pkg.build_id:
        say("Not verified (no build id in the package, or --no-verify).")
        return EXIT_OK
    rp.start_phase(5, "verify", "the bootloader activates the new app, which boots; its BLE starts about 30 s "
                                "later, then the build id is compared (normally 30-60 s, up to "
                                f"{args.verify_seconds:.0f} s)")
    await asyncio.sleep(5)  # the app boots; BLE starts 30 s after boot
    return EXIT_OK if await verify(cfg.node, pkg, args.verify_seconds, rp) else EXIT_FAILED


async def scan(seconds: float, adapter: str = "") -> list[tuple[str, str, str, int]]:
    """Passive scan: every SpArkNode-related advertiser as (kind, address, name, rssi), where kind
    is "app" (Nordic UART service) or "bootloader" (DFU service). Read-only."""
    from bleak import BleakScanner

    kw = {"cb": {"use_bdaddr": True}} if sys.platform == "darwin" else {}
    kw.update(bleak_args(adapter))
    seen: dict[str, tuple[str, str, str, int]] = {}

    def on_adv(device, adv):
        uuids = [u.lower() for u in adv.service_uuids or []]
        kind = "bootloader" if dfu.is_bootloader_adv(uuids, adv.local_name) else (
            "app" if dfu.NUS_SERVICE in uuids else "")
        if kind:
            seen[device.address] = (kind, device.address, adv.local_name or device.name or "", adv.rssi)

    async with BleakScanner(detection_callback=on_adv, **kw):
        await asyncio.sleep(seconds)
    return sorted(seen.values())


def scan_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="spark-bridge scan",
                                 description="Passive BLE scan: SpArkNode app (NUS) and DFU bootloader advertisers. "
                                             "A node connected to a host does not advertise.")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--ble-adapter", default=None, metavar="hciN|MAC", help=ADAPTER_HELP)
    args = ap.parse_args(argv)
    adapter = args.ble_adapter
    try:
        if adapter is None:
            try:
                adapter = load_env(None).node.ble_adapter  # the same .env as the bridge, if there is one
            except (ConfigError, OSError):
                adapter = ""
        adapter = check_adapter(adapter)
        rows = asyncio.run(scan(args.seconds, adapter))
    except (ConfigError, AdapterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for kind, addr, name, rssi in rows:
        print(f"{kind:10s} {addr:38s} {rssi:4d} dBm  {name}")
    if not rows:
        print("nothing advertising (a connected node does not advertise; is the bridge connected?)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # redirected to a file: still one flushed line at a time
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        say("Interrupted. If the transfer had started, the node has no application and waits in its "
            "bootloader: run again with --in-bootloader (SpArk bootloader), see spark/README.md 'Recovery'.")
        return EXIT_FAILED
