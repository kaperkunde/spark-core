"""spark-bridge entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .config import ConfigError, load, load_env


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="spark-bridge",
        description="Spark Bridge: connect a SparkNode (MeshCore room server with HostLink) to Discord. "
                    "Settings and DISCORD_TOKEN come from the repository's .env (see .env.example). "
                    "'spark-bridge update <zip>' updates the node over Bluetooth (see --help there).")
    ap.add_argument("--env", "-e", default=None,
                    help="path to the .env file (default: $SPARK_ENV_FILE, ./.env, or the repository's .env)")
    ap.add_argument("--config", "-c", default=None, help="legacy: a config.toml instead of .env")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--no-discord", action="store_true",
                    help="connect to the node and keep state, but do not start the Discord bot")
    ap.add_argument("--version", action="version", version=f"SparkBridge{__version__} (spark-bridge {__version__})")
    return ap


# Non-zero so supervisors restart the process (systemd Restart=always, launchd KeepAlive).
EXIT_LINK_GAVE_UP = 3


async def run(cfg, use_discord: bool) -> int:
    from .bridge import Bridge
    from .health import HealthMonitor
    from .lock import LinkLock, lock_path_for
    from .nodelink import LinkGaveUp, NodeLink
    from .store import Store

    store = Store(cfg.db_path)
    cfg.node.poll_seconds = cfg.health.sample_minutes * 60
    lock = LinkLock(lock_path_for(cfg.db_path))
    node = NodeLink(cfg.node, lock=lock)
    health = HealthMonitor(store, cfg.health)

    client = None
    if use_discord:
        from .discordbot import SparkClient
        client = SparkClient(cfg)
        chat = client.chat
    else:
        class _Null:
            async def send_as(self, *a): logging.getLogger("spark_bridge").info("send_as %s", a)
            async def react(self, *a): pass
            async def notice(self, *a): logging.getLogger("spark_bridge").info("notice %s", a)
        chat = _Null()
    bridge = Bridge(store, node, chat, cfg)
    bridge.health = health
    node.handlers = bridge

    tasks = [asyncio.create_task(node.run(), name="node")]
    if client:
        client.bridge = bridge
        tasks.append(asyncio.create_task(client.start(cfg.discord_token), name="discord"))
        tasks.append(asyncio.create_task(health.run(client.report_to_channels), name="health"))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:  # `spark-bridge update` asks for the link this way (see lock.py)
        loop.add_signal_handler(signal.SIGUSR1, node.request_pause)
    except (NotImplementedError, AttributeError):
        pass
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
    node.stop()
    code = 0
    for t in done:
        if t is not waiter and not t.cancelled() and t.exception():
            logging.getLogger("spark_bridge").error("task %s died: %r", t.get_name(), t.exception())
            code = EXIT_LINK_GAVE_UP if isinstance(t.exception(), LinkGaveUp) else 1
    if client:
        await client.close()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    store.close()
    lock.release()
    return code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["update"]:
        from .update import main as update_main
        return update_main(argv[1:])
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        cfg = load(args.config) if args.config else load_env(args.env)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if not args.no_discord and not cfg.discord_token:
        print("DISCORD_TOKEN is not set (.env or environment)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(cfg, not args.no_discord))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
