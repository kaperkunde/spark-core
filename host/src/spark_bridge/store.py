"""SQLite state: per-channel behaviour, dedup, names, battery samples, link events."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path

SEND_TARGETS = ("none", "public", "room", "both")
RECEIVE_SOURCES = ("none", "public", "room", "both")
RATE_MODES = ("none", "channel", "user")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
  channel_id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  admin INTEGER NOT NULL DEFAULT 0,
  send_target TEXT NOT NULL DEFAULT 'none',
  prefix INTEGER NOT NULL DEFAULT 1,
  rate_mode TEXT NOT NULL DEFAULT 'channel',
  rate_per_minute INTEGER NOT NULL DEFAULT 3,
  receive_source TEXT NOT NULL DEFAULT 'none',
  report INTEGER NOT NULL DEFAULT 0,
  updated_by INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS seen_posts(
  kind TEXT NOT NULL, ts INTEGER NOT NULL, author TEXT NOT NULL, text_hash TEXT NOT NULL,
  added INTEGER NOT NULL,
  PRIMARY KEY(kind, ts, author, text_hash)
);
CREATE TABLE IF NOT EXISTS sent_posts(text_hash TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS sent_posts_hash ON sent_posts(text_hash);
CREATE TABLE IF NOT EXISTS names(prefix TEXT PRIMARY KEY, name TEXT NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS battery(ts INTEGER PRIMARY KEY, mv INTEGER NOT NULL, uptime INTEGER, noise INTEGER);
CREATE TABLE IF NOT EXISTS link_events(ts INTEGER NOT NULL, up INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SEEN_KEEP = 500
SENT_KEEP_SECONDS = 24 * 3600


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: int
    guild_id: int = 0
    admin: bool = False
    send_target: str = "none"
    prefix: bool = True
    rate_mode: str = "channel"
    rate_per_minute: int = 3
    receive_source: str = "none"
    report: bool = False

    def sends_to(self, target: str) -> bool:
        return self.send_target in (target, "both")

    def receives(self, source: str) -> bool:
        return self.receive_source in (source, "both")

    def is_default(self, default_rate: int) -> bool:
        return (
            not self.admin
            and self.send_target == "none"
            and self.receive_source == "none"
            and not self.report
            and self.prefix
            and self.rate_mode == "channel"
            and self.rate_per_minute == default_rate
        )


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- channels -------------------------------------------------------
    @staticmethod
    def _row_to_cfg(r: sqlite3.Row) -> ChannelConfig:
        return ChannelConfig(
            channel_id=r["channel_id"],
            guild_id=r["guild_id"],
            admin=bool(r["admin"]),
            send_target=r["send_target"],
            prefix=bool(r["prefix"]),
            rate_mode=r["rate_mode"],
            rate_per_minute=r["rate_per_minute"],
            receive_source=r["receive_source"],
            report=bool(r["report"]),
        )

    def get_channel(self, channel_id: int) -> ChannelConfig | None:
        r = self.db.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
        return self._row_to_cfg(r) if r else None

    def all_channels(self) -> list[ChannelConfig]:
        return [self._row_to_cfg(r) for r in self.db.execute("SELECT * FROM channels ORDER BY channel_id")]

    def save_channel(self, cfg: ChannelConfig, user_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO channels(channel_id,guild_id,admin,send_target,prefix,rate_mode,"
            "rate_per_minute,receive_source,report,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET guild_id=excluded.guild_id, admin=excluded.admin, "
            "send_target=excluded.send_target, prefix=excluded.prefix, rate_mode=excluded.rate_mode, "
            "rate_per_minute=excluded.rate_per_minute, receive_source=excluded.receive_source, "
            "report=excluded.report, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (cfg.channel_id, cfg.guild_id, int(cfg.admin), cfg.send_target, int(cfg.prefix),
             cfg.rate_mode, cfg.rate_per_minute, cfg.receive_source, int(cfg.report),
             user_id, int(time.time())),
        )
        self.db.commit()

    def delete_channel(self, channel_id: int) -> bool:
        cur = self.db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
        self.db.commit()
        return cur.rowcount > 0

    def update_channel(self, channel_id: int, guild_id: int, default_rate: int = 3,
                       user_id: int | None = None, **changes) -> ChannelConfig:
        cur = self.get_channel(channel_id) or ChannelConfig(
            channel_id=channel_id, guild_id=guild_id, rate_per_minute=default_rate
        )
        new = replace(cur, guild_id=guild_id or cur.guild_id, **changes)
        self.save_channel(new, user_id)
        return new

    # --- dedup ----------------------------------------------------------
    def seen(self, kind: str, ts: int, author: str, text_hash: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM seen_posts WHERE kind=? AND ts=? AND author=? AND text_hash=?",
            (kind, ts, author, text_hash),
        ).fetchone() is not None

    def mark_seen(self, kind: str, ts: int, author: str, text_hash: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen_posts VALUES(?,?,?,?,?)",
            (kind, ts, author, text_hash, int(time.time())),
        )
        # keep the last SEEN_KEEP rows (by insertion order)
        self.db.execute(
            "DELETE FROM seen_posts WHERE rowid NOT IN "
            "(SELECT rowid FROM seen_posts ORDER BY rowid DESC LIMIT ?)", (SEEN_KEEP,))
        self.db.commit()

    def add_sent(self, text_hash: str, now: float | None = None) -> None:
        now = int(now if now is not None else time.time())
        self.db.execute("INSERT INTO sent_posts VALUES(?,?)", (text_hash, now))
        self.db.execute("DELETE FROM sent_posts WHERE ts<?", (now - SENT_KEEP_SECONDS,))
        self.db.commit()

    def was_sent(self, text_hash: str) -> bool:
        return self.db.execute("SELECT 1 FROM sent_posts WHERE text_hash=?", (text_hash,)).fetchone() is not None

    # --- names ----------------------------------------------------------
    def set_name(self, prefix: str, name: str) -> None:
        self.db.execute(
            "INSERT INTO names VALUES(?,?,?) ON CONFLICT(prefix) DO UPDATE SET name=excluded.name, updated=excluded.updated",
            (prefix.lower(), name, int(time.time())),
        )
        self.db.commit()

    def get_name(self, prefix: str) -> str | None:
        r = self.db.execute("SELECT name FROM names WHERE prefix=?", (prefix.lower(),)).fetchone()
        return r["name"] if r else None

    # --- battery / link -------------------------------------------------
    def add_battery(self, ts: int, mv: int, uptime: int | None = None, noise: int | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO battery VALUES(?,?,?,?)", (int(ts), mv, uptime, noise))
        self.db.commit()

    def battery_between(self, start: int, end: int) -> list[tuple[int, int]]:
        return [(r["ts"], r["mv"]) for r in self.db.execute(
            "SELECT ts, mv FROM battery WHERE ts BETWEEN ? AND ? ORDER BY ts", (int(start), int(end)))]

    def last_battery(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM battery ORDER BY ts DESC LIMIT 1").fetchone()

    def add_link_event(self, ts: int, up: bool) -> None:
        self.db.execute("INSERT INTO link_events VALUES(?,?)", (int(ts), int(up)))
        self.db.execute("DELETE FROM link_events WHERE ts<?", (int(ts) - 14 * 86400,))
        self.db.commit()

    def link_events_since(self, start: int) -> tuple[bool | None, list[tuple[int, bool]]]:
        """State at `start` (None if unknown) and the events after it."""
        before = self.db.execute(
            "SELECT up FROM link_events WHERE ts<=? ORDER BY ts DESC, rowid DESC LIMIT 1", (int(start),)).fetchone()
        evs = [(r["ts"], bool(r["up"])) for r in self.db.execute(
            "SELECT ts, up FROM link_events WHERE ts>? ORDER BY ts, rowid", (int(start),))]
        return (bool(before["up"]) if before else None), evs

    # --- meta -----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    def delete_meta(self, key: str) -> None:
        self.db.execute("DELETE FROM meta WHERE key=?", (key,))
        self.db.commit()
