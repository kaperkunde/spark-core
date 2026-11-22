"""Link lock, daemon pause and the `spark-bridge update` flow (stub node + fake bootloader)."""
import asyncio
import types

import pytest

from spark_bridge import update
from spark_bridge.config import NodeConfig
from spark_bridge.lock import LinkLock, lock_path_for
from spark_bridge.nodelink import NodeLink
from test_dfu import FakeDevice, FakeTransport, image, init_packet, zip_bytes

T0 = 1700000000
BUILD = "v1.18.0-SparkNode1.1 ab12cd34-261122190000"


async def wait_for(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_lock_exclusive(tmp_path):
    p = tmp_path / "spark-bridge.lock"
    a, b = LinkLock(p), LinkLock(p)
    assert a.try_acquire() and a.held
    assert not b.try_acquire()
    import os
    assert b.holder_pid() == os.getpid()
    a.release()
    assert b.try_acquire()
    b.release()
    assert lock_path_for("/x/y/spark-bridge.db").name == "spark-bridge.lock"


async def test_daemon_pauses_and_hands_over_the_lock(stub, tmp_path):
    lock = LinkLock(tmp_path / "l")
    cfg = NodeConfig(transport="tcp", tcp_port=stub.port, command_timeout=3, backoff_start=0.05,
                     backoff_max=0.2, poll_seconds=30, give_up_minutes=0.001, watchdog_seconds=0.05)
    link = NodeLink(cfg, None, clock=lambda: T0, lock=lock, pause_grace=0.1)
    task = asyncio.create_task(link.run())
    try:
        await wait_for(lambda: link.connected)
        assert lock.held
        link.request_pause()
        await wait_for(lambda: not lock.held and link.paused)
        assert not link.connected
        updater = LinkLock(tmp_path / "l")
        assert await updater.acquire(timeout=2)
        await asyncio.sleep(0.3)  # longer than give_up_minutes: a pause must not trigger the watchdog
        assert not task.done() and link.paused and not link.connected
        updater.release()
        await wait_for(lambda: link.connected and lock.held)
        assert not link.paused
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


async def test_daemon_waits_for_lock_at_start(stub, tmp_path):
    other = LinkLock(tmp_path / "l")
    assert other.try_acquire()
    cfg = NodeConfig(transport="tcp", tcp_port=stub.port, command_timeout=3, backoff_start=0.05)
    link = NodeLink(cfg, None, clock=lambda: T0, lock=LinkLock(tmp_path / "l"))
    task = asyncio.create_task(link.run())
    try:
        await asyncio.sleep(0.3)
        assert not link.connected and link.paused
        other.release()
        await wait_for(lambda: link.connected)
    finally:
        link.stop()
        await asyncio.wait_for(task, 5)


def test_parse_build():
    assert update.parse_build("> " + BUILD) == BUILD
    assert update.parse_build("ERR: unknown command") is None


def test_bluez_remove_never_removes_the_app_device(monkeypatch):
    calls = []
    monkeypatch.setattr(update.sys, "platform", "linux")
    monkeypatch.setattr(update.shutil, "which", lambda _n: "/usr/bin/bluetoothctl")
    monkeypatch.setattr(update.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    update.bluez_remove("C0:00:00:00:00:11", "C0:00:00:00:00:11")
    update.bluez_remove("not-a-mac", "")
    assert calls == []
    update.bluez_remove("C0:00:00:00:00:11", "C0:00:00:00:00:10")
    assert calls == [["bluetoothctl", "remove", "C0:00:00:00:00:11"]]


# --- the whole flow: stub node (app, HostLink over TCP) + fake bootloader ---------------------------

def env_file(tmp_path, port):
    p = tmp_path / ".env"
    p.write_text(f"BRIDGE_TRANSPORT=tcp\nBRIDGE_TCP_PORT={port}\nBRIDGE_DB_PATH={tmp_path}/b.db\n")
    return str(p)


def write_zip(tmp_path):
    img = image(4000)
    p = tmp_path / "fw.zip"
    p.write_bytes(zip_bytes(img, init_packet(img)))
    return str(p), img


def args_for(tmp_path, port, zip_path, **kw):
    a = update.build_parser().parse_args([zip_path, "--env", env_file(tmp_path, port), "--yes",
                                          "--scan-seconds", "1", "--verify-seconds", "3", "--lock-timeout", "1"])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


@pytest.fixture
def fake_ble(monkeypatch):
    dev = FakeDevice(spark=True)
    found = []

    async def find_bootloader(address, timeout):
        found.append(address)
        if dev.mode != "ota":
            raise update.dfu.DfuError("no DFU bootloader advertising")
        return dev

    class T(FakeTransport):
        def __init__(self, d, **kw):
            super().__init__(d)

    monkeypatch.setattr(update, "find_bootloader", find_bootloader)
    monkeypatch.setattr(update.dfu, "BleakTransport", T)
    monkeypatch.setattr(update.asyncio, "sleep", _fast_sleep)
    return types.SimpleNamespace(dev=dev, found=found)


_real_sleep = asyncio.sleep


async def _fast_sleep(s, *a, **kw):
    await _real_sleep(min(s, 0.01))


async def test_update_flow(stub, tmp_path, fake_ble):
    stub.cli_replies["get build"] = "> v1.18.0-SparkNode1.1 00000000-261101000000"
    stub.cli_replies["dfu"] = "OK - dfu C0:11:22:33:44:56 in 2 s, battery 4012 mV, x"
    zip_path, img = write_zip(tmp_path)

    def after_activate():  # the new app reports the package's build
        stub.cli_replies["get build"] = "> " + BUILD

    orig = FakeTransport.write_control

    async def wc(self, data):
        await orig(self, data)
        if data[:1] == b"\x05":
            after_activate()

    FakeTransport.write_control = wc
    try:
        code = await update.run(args_for(tmp_path, stub.port, zip_path))
    finally:
        FakeTransport.write_control = orig
    assert code == update.EXIT_OK
    assert "dfu" in stub.cli_log and "dfu force" not in stub.cli_log
    assert fake_ble.found == ["C0:11:22:33:44:56"]
    assert fake_ble.dev.app == img
    assert not LinkLock(tmp_path / "spark-bridge.lock").held  # released


async def test_update_refuses_low_battery(stub, tmp_path, fake_ble):
    stub.cli_replies["get build"] = "> v1.18.0-SparkNode1.1 00000000-261101000000"
    stub.battery_mv = 3500
    zip_path, _ = write_zip(tmp_path)
    assert await update.run(args_for(tmp_path, stub.port, zip_path)) == update.EXIT_FAILED
    assert "dfu" not in stub.cli_log and fake_ble.found == []


async def test_update_refuses_sparknode1(stub, tmp_path, fake_ble):
    zip_path, _ = write_zip(tmp_path)  # the stub answers "OK" to get build, like an unknown command
    stub.cli_replies["get build"] = "Unknown command"
    assert await update.run(args_for(tmp_path, stub.port, zip_path)) == update.EXIT_FAILED
    assert "dfu" not in stub.cli_log


async def test_update_refuses_same_build(stub, tmp_path, fake_ble):
    stub.cli_replies["get build"] = "> " + BUILD
    zip_path, _ = write_zip(tmp_path)
    assert await update.run(args_for(tmp_path, stub.port, zip_path)) == update.EXIT_FAILED
    assert "dfu" not in stub.cli_log


async def test_update_reports_no_app_when_transfer_breaks(stub, tmp_path, fake_ble):
    stub.cli_replies["get build"] = "> v1.18.0-SparkNode1.1 00000000-261101000000"
    stub.cli_replies["dfu"] = "OK - dfu C0:11:22:33:44:56 in 2 s, battery 4012 mV, x"
    fake_ble.dev.crc_fail = True
    zip_path, _ = write_zip(tmp_path)
    code = await update.run(args_for(tmp_path, stub.port, zip_path, attempts=2))
    assert code == update.EXIT_NO_APP
    assert fake_ble.dev.mode == "ota"  # Spark bootloader: still waiting for a retry


async def test_update_waits_for_lock(stub, tmp_path, fake_ble):
    holder = LinkLock(tmp_path / "spark-bridge.lock")
    assert holder.try_acquire()  # same pid: no signal is sent, the lock is simply busy
    zip_path, _ = write_zip(tmp_path)
    try:
        assert await update.run(args_for(tmp_path, stub.port, zip_path)) == update.EXIT_FAILED
    finally:
        holder.release()
