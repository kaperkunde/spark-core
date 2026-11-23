"""Link lock, daemon pause and the `spark-bridge update` flow (stub node + fake bootloader)."""
import asyncio
import json
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


# --- progress reporting ---------------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_fmt_duration_and_progress_line():
    assert update.fmt_duration(134) == "2:14"
    assert update.fmt_duration(3725) == "1:02:05"
    assert update.fmt_duration(None) == "--:--"
    line = update.fmt_progress(176_000, 391_356, 1310.0, 134)
    assert line == "transfer 44%  176k/391k bytes  1.31 kB/s  elapsed 2:14  ETA 2:44"
    assert "-- kB/s" in update.fmt_progress(0, 1000, None, 0) and "ETA --:--" in update.fmt_progress(0, 1000, None, 0)


def test_reporter_throttle_window_rate_and_eta(capsys):
    clk = FakeClock()
    rp = update.Reporter(clock=clk)
    total = 100_000
    rp._reset_transfer()
    done = 0
    for _ in range(40):  # 1 kB/s for 20 s, then 4 kB/s: the window must follow the recent speed
        clk.t += 1
        done += 1000
        rp.progress(done, total)
    for _ in range(40):
        clk.t += 1
        done += 4000 if done < total else 0
        rp.progress(min(done, total), total)
    out = capsys.readouterr().out.splitlines()
    assert all(l[2] == ":" and l[5] == ":" for l in out)  # every line has HH:MM:SS
    pcts = [int(l.split("transfer ")[1].split("%")[0]) for l in out]
    assert pcts == sorted(pcts) and pcts[-1] == 100
    assert len(out) < 25  # throttled: 80 callbacks, far fewer lines
    assert "\r" not in "".join(out)


def test_reporter_time_trigger_and_rate(capsys):
    clk = FakeClock()
    rp = update.Reporter(clock=clk)
    rp._reset_transfer()
    for i in range(1, 40):  # 100 B/s: 5 % never reached in 15 s, so the 15 s rule prints
        clk.t += 1
        rp.progress(i * 100, 1_000_000)
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "0.10 kB/s" in out[0] and "elapsed 0:15" in out[0]


def test_reporter_restarts_on_retry(capsys):
    clk = FakeClock()
    rp = update.Reporter(clock=clk)
    rp._reset_transfer()
    for d in (10_000, 60_000):
        clk.t += 10
        rp.progress(d, 100_000)
    clk.t += 5
    rp.progress(5_000, 100_000)  # attempt 2 starts from 0 again
    assert rp.samples[0][1] == 0 and rp.samples[-1][1] == 5_000
    capsys.readouterr()


def test_status_file_is_atomic_json_and_failure_hint(tmp_path):
    clk = FakeClock()
    sf = tmp_path / "st.json"
    rp = update.Reporter(str(sf), "spark-bridge update fw.zip --yes", clock=clk)
    rp.bl_mac = "C0:11:22:33:44:56"
    rp.start_phase(4, "transfer", "sending")
    clk.t += 10
    rp.progress(50_000, 100_000)
    d = json.loads(sf.read_text())
    assert (d["state"], d["phase"], d["step"], d["pct"], d["bytes"], d["total"]) == \
        ("running", "transfer", 4, 50, 50_000, 100_000)
    assert d["rate"] and d["eta_s"] and d["updated_at"] and d["resume_hint"] == ""
    rp.app_erased = True
    rp.finish(update.EXIT_NO_APP)
    d = json.loads(sf.read_text())
    assert d["state"] == "failed"
    assert d["resume_hint"] == "spark-bridge update fw.zip --yes --in-bootloader --bootloader-address C0:11:22:33:44:56"
    assert not (tmp_path / "st.json.tmp").exists()
    rp.build = "v1 x"
    rp.finish(update.EXIT_OK)
    d = json.loads(sf.read_text())
    assert d["state"] == "done" and d["pct"] == 100 and d["eta_s"] == 0 and d["build"] == "v1 x"


async def test_update_flow_reports_phases_and_status(stub, tmp_path, fake_ble, capsys):
    stub.cli_replies["get build"] = "> v1.18.0-SparkNode1.1 00000000-261101000000"
    stub.cli_replies["dfu"] = "OK - dfu C0:11:22:33:44:56 in 2 s, battery 4012 mV, x"
    zip_path, _ = write_zip(tmp_path)
    sf = tmp_path / "status.json"
    orig = FakeTransport.write_control

    async def wc(self, data):
        await orig(self, data)
        if data[:1] == b"\x05":
            stub.cli_replies["get build"] = "> " + BUILD

    FakeTransport.write_control = wc
    try:
        code = await update.run(args_for(tmp_path, stub.port, zip_path, status_file=str(sf)))
    finally:
        FakeTransport.write_control = orig
    out = capsys.readouterr().out
    assert code == update.EXIT_OK
    for n in range(1, 6):
        assert f"[{n}/5]" in out
    assert "Done: update finished in" in out and BUILD.split()[0] in out
    d = json.loads(sf.read_text())
    assert d["state"] == "done" and d["build"].startswith(BUILD.split()[0])
    print(out)


async def test_update_failure_prints_resume_command(stub, tmp_path, fake_ble, capsys):
    stub.cli_replies["get build"] = "> v1.18.0-SparkNode1.1 00000000-261101000000"
    stub.cli_replies["dfu"] = "OK - dfu C0:11:22:33:44:56 in 2 s, battery 4012 mV, x"
    fake_ble.dev.crc_fail = True
    zip_path, _ = write_zip(tmp_path)
    sf = tmp_path / "status.json"
    code = await update.run(args_for(tmp_path, stub.port, zip_path, attempts=2, status_file=str(sf)))
    out = capsys.readouterr().out
    assert code == update.EXIT_NO_APP
    assert "FAILED in phase 'transfer'" in out and "--in-bootloader --bootloader-address C0:11:22:33:44:56" in out
    assert json.loads(sf.read_text())["state"] == "failed"
