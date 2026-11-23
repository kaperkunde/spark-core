"""Configuration: the repository's single .env file (see .env.example at the top level).

The bridge reads the BRIDGE_* keys, DISCORD_TOKEN and SPARK_NODE_NAME (default BLE name).
Variables already set in the process environment win over the file. The older TOML format
(--config config.toml, with DISCORD_TOKEN in a .env next to it) is still accepted.

There is deliberately no BLE PIN key: pairing happens once at OS level.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from .observer import ObserverConfig, ObserverError, parse_brokers


class ConfigError(Exception):
    pass


@dataclass
class NodeConfig:
    transport: str = "ble"  # ble | serial | tcp
    ble_name: str = "SparkNode"  # substring of the BLE name ("MeshCore-SparkNode")
    ble_address: str = ""  # optional; wins over ble_name when set
    serial_port: str = ""
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 5000
    command_timeout: float = 10.0
    poll_seconds: float = 600.0  # set from [health] sample_minutes by __main__
    poll_failures: int = 2
    backoff_start: float = 2.0
    backoff_max: float = 60.0
    # BLE: after this many consecutive failed connection attempts, stop scanning and try the
    # OS's already-connected peripherals (macOS/BlueZ) before the next scan.
    ble_direct_after: int = 3
    # Exit non-zero after this many minutes without a link so a supervisor (systemd
    # Restart=always, launchd KeepAlive) restarts the process. 0 disables.
    give_up_minutes: float = 10.0
    # Overall limit for one connect + handshake (create_ble, APP_START, queries, contacts). A BLE
    # connect to a marginal node can await forever; a timeout counts as a failed attempt.
    connect_timeout: float = 45.0
    # How often the watchdog checks the time without a link (it enforces give_up_minutes even
    # if a connect attempt never returns).
    watchdog_seconds: float = 5.0


@dataclass
class DiscordConfig:
    allowed_guild_ids: list[int] = field(default_factory=list)
    webhook_name: str = "Spark Bridge"


@dataclass
class LimitsConfig:
    # Global safety cap on Public transmissions (radio duty cycle), all channels together.
    # 0 disables the cap.
    public_per_minute: int = 3
    public_burst: int = 3
    # Default for newly configured channels: per-channel messages per minute.
    default_per_minute: int = 3


@dataclass
class HealthConfig:
    tz: str = "UTC"  # IANA name (.env BRIDGE_TZ)
    dawn: time = time(7, 0)
    afternoon: time = time(15, 0)
    report: time = time(15, 5)
    sample_minutes: int = 10


@dataclass
class Config:
    node: NodeConfig = field(default_factory=NodeConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    observer: ObserverConfig = field(default_factory=ObserverConfig)
    observer_password: str = ""   # value of the variable named by observer.password_env
    db_path: str = "spark-bridge.db"
    allow_dangerous_cli: bool = False
    discord_token: str = ""
    config_dir: Path = Path(".")


def _hhmm(value: str, key: str) -> time:
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{key}: expected HH:MM, got {value!r}") from exc


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


def _fill(obj, data: dict, section: str) -> None:
    for key, val in data.items():
        if not hasattr(obj, key):
            raise ConfigError(f"unknown key [{section}] {key}")
        setattr(obj, key, val)


def parse(data: dict, config_dir: Path = Path(".")) -> Config:
    cfg = Config(config_dir=config_dir)
    known = {"node", "discord", "limits", "health", "state", "observer", "allow_dangerous_cli"}
    for key in data:
        if key not in known:
            raise ConfigError(f"unknown section or key: {key}")
    _fill(cfg.node, data.get("node", {}), "node")
    _fill(cfg.discord, data.get("discord", {}), "discord")
    _fill(cfg.limits, data.get("limits", {}), "limits")
    h = dict(data.get("health", {}))
    for k in ("dawn", "afternoon", "report"):
        if k in h:
            h[k] = _hhmm(h[k], f"[health] {k}")
    _fill(cfg.health, h, "health")
    _fill(cfg.observer, data.get("observer", {}), "observer")
    if isinstance(cfg.observer.brokers, str):
        cfg.observer.brokers = _list(cfg.observer.brokers)
    if cfg.observer.enabled:
        if not cfg.observer.iata.strip():
            raise ConfigError("[observer] iata (region code, e.g. AMS) is required when the observer is on")
        try:
            parse_brokers(cfg.observer)
        except ObserverError as exc:
            raise ConfigError(f"[observer] {exc}") from exc
    state = data.get("state", {})
    if "db_path" in state:
        cfg.db_path = state["db_path"]
    cfg.allow_dangerous_cli = bool(data.get("allow_dangerous_cli", False))

    if cfg.node.transport not in ("ble", "serial", "tcp"):
        raise ConfigError("[node] transport must be ble, serial or tcp")
    if cfg.node.transport == "serial" and not cfg.node.serial_port:
        raise ConfigError("[node] serial_port is required for transport = serial")
    if cfg.node.transport == "ble" and not (cfg.node.ble_name or cfg.node.ble_address):
        raise ConfigError("[node] ble_name or ble_address is required for transport = ble")
    cfg.discord.allowed_guild_ids = [int(g) for g in cfg.discord.allowed_guild_ids]
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(cfg.health.tz)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"[health] tz: unknown timezone {cfg.health.tz!r}") from exc
    return cfg


def load(path: str | Path, env: bool = True) -> Config:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {p}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: {exc}") from exc
    cfg = parse(data, p.resolve().parent)
    if env:
        load_dotenv(cfg.config_dir / ".env")
        cfg.discord_token = os.environ.get("DISCORD_TOKEN", "")
        if cfg.observer.password_env:
            cfg.observer_password = os.environ.get(cfg.observer.password_env, "")
    db = Path(cfg.db_path)
    if not db.is_absolute():
        cfg.db_path = str(cfg.config_dir / db)
    return cfg


# --- .env -----------------------------------------------------------------------------------

def _list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _ids(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


def _bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(value)


# .env key -> (section, field, parser). Empty values keep the default.
ENV_KEYS: dict[str, tuple[str, str, object]] = {
    "BRIDGE_TRANSPORT": ("node", "transport", str),
    "BRIDGE_BLE_NAME": ("node", "ble_name", str),
    "BRIDGE_BLE_ADDRESS": ("node", "ble_address", str),
    "BRIDGE_SERIAL_PORT": ("node", "serial_port", str),
    "BRIDGE_TCP_HOST": ("node", "tcp_host", str),
    "BRIDGE_TCP_PORT": ("node", "tcp_port", int),
    "BRIDGE_GIVE_UP_MINUTES": ("node", "give_up_minutes", float),
    "BRIDGE_CONNECT_TIMEOUT": ("node", "connect_timeout", float),
    "BRIDGE_WATCHDOG_SECONDS": ("node", "watchdog_seconds", float),
    "BRIDGE_BLE_DIRECT_AFTER": ("node", "ble_direct_after", int),
    "BRIDGE_POLL_FAILURES": ("node", "poll_failures", int),
    "BRIDGE_GUILD_IDS": ("discord", "allowed_guild_ids", _ids),
    "BRIDGE_WEBHOOK_NAME": ("discord", "webhook_name", str),
    "BRIDGE_PUBLIC_PER_MINUTE": ("limits", "public_per_minute", int),
    "BRIDGE_PUBLIC_BURST": ("limits", "public_burst", int),
    "BRIDGE_DEFAULT_PER_MINUTE": ("limits", "default_per_minute", int),
    "BRIDGE_TZ": ("health", "tz", str),
    "BRIDGE_HEALTH_DAWN": ("health", "dawn", str),
    "BRIDGE_HEALTH_AFTERNOON": ("health", "afternoon", str),
    "BRIDGE_HEALTH_REPORT": ("health", "report", str),
    "BRIDGE_HEALTH_SAMPLE_MINUTES": ("health", "sample_minutes", int),
    "BRIDGE_DB_PATH": ("state", "db_path", str),
    "BRIDGE_ALLOW_DANGEROUS_CLI": ("", "allow_dangerous_cli", _bool),
    # MQTT observer (see observer.py and host/README.md)
    "BRIDGE_OBSERVER": ("observer", "enabled", _bool),
    "BRIDGE_OBSERVER_BROKERS": ("observer", "brokers", _list),
    "BRIDGE_OBSERVER_IATA": ("observer", "iata", str),
    "BRIDGE_OBSERVER_NAME": ("observer", "name", str),
    "BRIDGE_OBSERVER_OWNER": ("observer", "owner", str),
    "BRIDGE_OBSERVER_EMAIL": ("observer", "email", str),
    "BRIDGE_OBSERVER_AUTH": ("observer", "auth", str),
    "BRIDGE_OBSERVER_USERNAME": ("observer", "username", str),
    "BRIDGE_OBSERVER_PASSWORD_ENV": ("observer", "password_env", str),
    "BRIDGE_OBSERVER_WS_PATH": ("observer", "ws_path", str),
    "BRIDGE_OBSERVER_TLS_VERIFY": ("observer", "tls_verify", _bool),
    "BRIDGE_OBSERVER_TOKEN_MINUTES": ("observer", "token_minutes", int),
    "BRIDGE_OBSERVER_QUEUE": ("observer", "queue_size", int),
}


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines (comments, blank lines and `export ` allowed, optional quotes)."""
    vals: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key:
            vals[key] = val
    return vals


def from_env(values: dict[str, str], config_dir: Path = Path(".")) -> Config:
    """Build a Config from .env-style values (already merged with the process environment)."""
    data: dict = {}
    for key, (section, fld, conv) in ENV_KEYS.items():
        raw = values.get(key, "")
        if raw == "":
            continue
        try:
            val = conv(raw)  # type: ignore[operator]
        except ValueError as exc:
            raise ConfigError(f"{key}: invalid value {raw!r}") from exc
        if section:
            data.setdefault(section, {})[fld] = val
        else:
            data[fld] = val
    if not values.get("BRIDGE_BLE_NAME") and values.get("SPARK_NODE_NAME"):
        data.setdefault("node", {})["ble_name"] = values["SPARK_NODE_NAME"]
    cfg = parse(data, config_dir)
    cfg.discord_token = values.get("DISCORD_TOKEN", "")
    if cfg.observer.password_env:
        cfg.observer_password = values.get(cfg.observer.password_env, "")
    return cfg


def default_env_path() -> Path:
    """$SPARK_ENV_FILE, else ./.env, else the repository's .env (host/src/spark_bridge/../../..)."""
    if os.environ.get("SPARK_ENV_FILE"):
        return Path(os.environ["SPARK_ENV_FILE"])
    here = Path(".env")
    if here.is_file():
        return here
    return Path(__file__).resolve().parents[3] / ".env"


def load_env(path: str | Path | None = None, environ: dict[str, str] | None = None) -> Config:
    p = Path(path) if path else default_env_path()
    if not p.is_file():
        raise ConfigError(f"{p} not found: run ./setup.sh or copy .env.example to .env")
    values = read_env_file(p)
    env = os.environ if environ is None else environ
    for key in list(ENV_KEYS) + ["DISCORD_TOKEN", "SPARK_NODE_NAME"]:
        if env.get(key):
            values[key] = env[key]
    pw_env = values.get("BRIDGE_OBSERVER_PASSWORD_ENV", "")
    if pw_env and env.get(pw_env):
        values[pw_env] = env[pw_env]
    cfg = from_env(values, p.resolve().parent)
    db = Path(cfg.db_path)
    if not db.is_absolute():
        cfg.db_path = str(cfg.config_dir / db)
    return cfg
