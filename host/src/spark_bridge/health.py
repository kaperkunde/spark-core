"""Battery sampling, the daily health line and the on-demand report."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import HealthConfig
from .store import Store

log = logging.getLogger("spark_bridge.health")
WINDOW = 90 * 60  # nearest sample must be within 90 minutes


def fmt_uptime(secs: int | None) -> str:
    if secs is None:
        return "n/a"
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h {r // 60}m"


class HealthMonitor:
    def __init__(self, store: Store, cfg: HealthConfig, clock: Callable[[], float] = time.time):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.tz = ZoneInfo(cfg.tz)
        self.last_sample: dict[str, Any] | None = None

    # --- sampling -------------------------------------------------------------
    def record(self, sample: dict[str, Any]) -> None:
        self.last_sample = sample
        self.store.add_battery(int(self.clock()), sample["battery_mv"], sample.get("uptime_secs"),
                               sample.get("noise_floor"))

    # --- pure helpers (tested with a fake clock) -------------------------------
    def _at(self, day: datetime, t: dtime) -> datetime:
        return datetime.combine(day.date(), t, tzinfo=self.tz)

    def nearest(self, target: datetime) -> int | None:
        ts = int(target.timestamp())
        rows = self.store.battery_between(ts - WINDOW, ts + WINDOW)
        if not rows:
            return None
        return min(rows, key=lambda r: abs(r[0] - ts))[1]

    def availability(self, now: float) -> float | None:
        start = int(now) - 86400
        state, events = self.store.link_events_since(start)
        if state is None and not events:
            return None
        up_secs, known = 0.0, 0.0
        cur_t, cur = float(start), state
        for ts, up in events:
            if cur is not None:
                known += ts - cur_t
                up_secs += (ts - cur_t) if cur else 0
            cur_t, cur = float(ts), up
        if cur is not None:
            known += now - cur_t
            up_secs += (now - cur_t) if cur else 0
        return up_secs / known if known > 0 else None

    def build_report(self, now: float | None = None, live: dict[str, Any] | None = None) -> str:
        now = self.clock() if now is None else now
        local = datetime.fromtimestamp(now, self.tz)
        dawn = self.nearest(self._at(local, self.cfg.dawn))
        aft = self.nearest(self._at(local, self.cfg.afternoon))
        y_aft = self.nearest(self._at(local - timedelta(days=1), self.cfg.afternoon))
        mv = lambda v: "n/a" if v is None else f"{v} mV"  # noqa: E731
        delta = "n/a" if dawn is None or y_aft is None else f"{dawn - y_aft:+d} mV"
        s = live or self.last_sample or {}
        row = self.store.last_battery()
        uptime = s.get("uptime_secs", row["uptime"] if row else None)
        noise = s.get("noise_floor", row["noise"] if row else None)
        avail = self.availability(now)
        lines = [
            f"**Spark health** ({local:%Y-%m-%d %H:%M} {self.cfg.tz})",
            f"- battery {self.cfg.dawn:%H:%M}: {mv(dawn)}",
            f"- battery {self.cfg.afternoon:%H:%M}: {mv(aft)}",
            f"- overnight change (today's dawn minus yesterday's afternoon): {delta}",
            f"- uptime: {fmt_uptime(uptime)}",
            f"- noise floor: {'n/a' if noise is None else f'{noise} dBm'}",
            f"- link availability (24 h): {'n/a' if avail is None else f'{avail * 100:.1f}%'}",
        ]
        if live:
            lines.append(f"- now: {live['battery_mv']} mV, last RSSI {live.get('last_rssi', 'n/a')} dBm, "
                         f"last SNR {live.get('last_snr', 'n/a')} dB")
        return "\n".join(lines)

    def next_report(self, now: float) -> float:
        local = datetime.fromtimestamp(now, self.tz)
        t = self._at(local, self.cfg.report)
        if t.timestamp() <= now:
            t = self._at(local + timedelta(days=1), self.cfg.report)
        return t.timestamp()

    # --- loop -----------------------------------------------------------------
    async def run(self, send: Callable[[str], Any], sleep=asyncio.sleep) -> None:
        """Send the daily line at the configured time. `send` posts to the report channels."""
        while True:
            await sleep(max(1.0, self.next_report(self.clock()) - self.clock()))
            try:
                await send(self.build_report())
            except Exception:  # noqa: BLE001
                log.exception("daily health report failed")
