#!/usr/bin/env python3
"""Run one command on a MeshCore node's USB text console and print the reply.

    node_cli.py --port /dev/ttyACM0 "get public.key"
    node_cli.py --port /dev/cu.usbmodem1101 --public-key     # prints only the 64 hex digits

Needs pyserial (setup.sh installs it with esptool in .spark-tools/venv). The console is the
one the room server offers over USB at 115200 baud; replies start with "-> ".
"""
from __future__ import annotations

import argparse
import re
import sys
import time


def run(port: str, command: str, wait: float) -> str:
    import serial  # pyserial

    with serial.Serial(port, 115200, timeout=0.2) as s:
        time.sleep(0.3)
        s.reset_input_buffer()
        s.write(b"\r")
        time.sleep(0.3)
        s.reset_input_buffer()
        s.write(command.encode() + b"\r")
        out = b""
        end = time.time() + wait
        while time.time() < end:
            out += s.read(512)
        return out.decode(errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--wait", type=float, default=2.0, help="seconds to collect the reply")
    ap.add_argument("--public-key", action="store_true", help="run 'get public.key' and print just the key")
    ap.add_argument("command", nargs="?", default="")
    a = ap.parse_args()
    if a.public_key:
        for _ in range(3):   # the node may still be booting
            m = re.search(r"\b([0-9A-Fa-f]{64})\b", run(a.port, "get public.key", a.wait))
            if m:
                print(m.group(1).upper())
                return 0
            time.sleep(2)
        print("no public key in the reply (is this a SparkNode console?)", file=sys.stderr)
        return 1
    if not a.command:
        ap.error("give a command or --public-key")
    print(run(a.port, a.command, a.wait), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
