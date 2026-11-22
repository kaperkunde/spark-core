#!/usr/bin/env bash
# SparkNode BLE OTA: guided hardware test (spark/OTA-TESTS.md has the same checklist).
#
#   spark/tools/ota_hwtest.sh --port /dev/cu.usbmodem1101 [--rollback SparkNode1.uf2] [--from STEP]
#
# Run it with the node ON THE DESK, connected over USB, batteries in. Every step says what to do,
# asks before running anything that writes to the node, and records pass / fail / skip with a
# note in out/ota-test/hwtest-<date>.log. Steps, in this order:
#   P0 build artifacts + unit tests (T0)     H1 read the installed bootloader (INFO_UF2.TXT)
#   H2 install the OTA-safe bootloader (USB) H3 flash SparkNode1.1 over USB
#   T1 passive scan, app                     T2 dfu over USB, bootloader at MAC+1, reset back
#   T4 spark-bridge update x5 (same/new)     T5 abort before Start, finish with --in-bootloader
#   T6 abort after Start: retry, power-cycle, USB recovery
#   T7 Service Changed (bonded reconnects)   T8 Pi at its final place x5   T9 phone fallback
# The bridge service is stopped for the scans (a connected node does not advertise) and is
# otherwise paused by spark-bridge update itself.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/out/ota-test"
PY="$ROOT/host/.venv/bin/python"
BRIDGE="$ROOT/host/.venv/bin/spark-bridge"
PIO="$(command -v pio || echo "$HOME/.platformio/penv/bin/pio")"
BL_UF2="$ROOT/out/spark/bootloader/update-xiao_nrf52840_ble_bootloader-0.11.0-spark1_nosd.uf2"
PORT="" ROLLBACK="" FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) shift; PORT="$1" ;;
    --rollback) shift; ROLLBACK="$1" ;;
    --from) shift; FROM="$1" ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$PORT" ] || { echo "--port <node USB serial port> is required" >&2; exit 2; }
[ -x "$PY" ] || { echo "host/.venv missing: ./setup.sh (Spark Bridge) or see host/README.md" >&2; exit 2; }
mkdir -p "$OUT"
LOG="$OUT/hwtest-$(date +%Y%m%d-%H%M%S).log"
echo "SparkNode OTA hardware test $(date)  port=$PORT" > "$LOG"

skipping=0; [ -n "$FROM" ] && skipping=1
step() {  # step ID "title"; returns 1 if skipped by --from
  CUR="$1"
  if [ "$skipping" = 1 ]; then [ "$1" = "$FROM" ] && skipping=0 || return 1; fi
  printf '\n\033[1m== %s  %s ==\033[0m\n' "$1" "$2"
  echo "== $1 $2" >> "$LOG"
}
say() { printf '%s\n' "$*"; echo "   $*" >> "$LOG"; }
ask() { local a; read -r -p "$1 [y/N] " a; case "$a" in [Yy]*) return 0 ;; *) return 1 ;; esac; }
pause() { read -r -p "$1 (Enter) " _; }
result() {  # result: asks pass/fail/skip and a note
  local r n
  read -r -p "$CUR result (p)ass / (f)ail / (s)kip: " r
  read -r -p "note: " n
  case "$r" in p*) r=PASS ;; f*) r=FAIL ;; *) r=SKIP ;; esac
  echo "$CUR $r $n" | tee -a "$LOG"
}
run() { say "+ $*"; "$@" 2>&1 | tee -a "$LOG"; return "${PIPESTATUS[0]}"; }
cli() { run "$PY" "$ROOT/spark/tools/node_cli.py" --port "$PORT" --wait 3 "$1"; }
drive() {
  local d
  for d in /Volumes/XIAO-BOOT* /media/"$USER"/XIAO-BOOT* /run/media/"$USER"/XIAO-BOOT*; do
    [ -f "$d/INFO_UF2.TXT" ] && { printf '%s' "$d"; return 0; }
  done
  return 1
}
wait_drive() {
  local i d
  pause "Double-press the node's reset button now"
  for i in $(seq 1 60); do d="$(drive)" && { printf '%s' "$d"; return 0; }; sleep 1; done
  return 1
}
zip_build() { "$PY" -c "import sys; from spark_bridge.dfu import load_package as l; print(l(sys.argv[1]).build)" "$1"; }
stop_bridge() {
  if [ "$(uname -s)" = Darwin ]; then
    say "Stop the bridge agent if it runs: launchctl unload ~/Library/LaunchAgents/com.kaperkunde.spark-bridge.plist"
  else
    say "Stop the bridge service if it runs: sudo systemctl stop spark-bridge"
  fi
  pause "Bridge stopped (or not running)?"
}

# --------------------------------------------------------------------------------------------
if step P0 "Build the artifacts and run the unit tests (T0)"; then
  say "Builds with the repository's .env (your real node settings)."
  if ask "Build SparkNode1.1 (UF2 + DFU zip), a second build with a new build id, and the bootloader?"; then
    (cd "$ROOT" && run "$PIO" run -e SparkNode_P1 -t create_uf2 -t dfu_zip) || exit 1
    cp "$ROOT/.pio/build/SparkNode_P1/firmware.uf2" "$OUT/SparkNode1.1.uf2"
    cp "$ROOT/.pio/build/SparkNode_P1/firmware-dfu.zip" "$OUT/SparkNode1.1.zip"
    (cd "$ROOT" && SPARK_BUILD_SALT="b-$(date +%s)" run "$PIO" run -e SparkNode_P1 -t create_uf2 -t dfu_zip) || exit 1
    cp "$ROOT/.pio/build/SparkNode_P1/firmware.uf2" "$OUT/SparkNode1.1b.uf2"
    cp "$ROOT/.pio/build/SparkNode_P1/firmware-dfu.zip" "$OUT/SparkNode1.1b.zip"
    run "$ROOT/spark/bootloader/build.sh" || exit 1
  fi
  say "SparkNode1.1:  $(zip_build "$OUT/SparkNode1.1.zip")"
  say "SparkNode1.1b: $(zip_build "$OUT/SparkNode1.1b.zip")"
  [ -n "$ROLLBACK" ] && say "rollback UF2: $ROLLBACK" || say "No --rollback UF2 given: keep the current SparkNode1 UF2 at hand."
  if ask "Run the unit tests (native_spark, pytest)?"; then
    (cd "$ROOT" && run "$PIO" test -e native_spark) && (cd "$ROOT/host" && run .venv/bin/pytest -q)
  fi
  result
fi

if step H1 "Read the installed bootloader (answers the plan's open question 1)"; then
  d="$(wait_drive)" || { say "no XIAO-BOOT drive"; result; exit 1; }
  run cat "$d/INFO_UF2.TXT"
  grep -q "Board-ID: nRF52840-SeeedXiao-v1" "$d/INFO_UF2.TXT" \
    && say "Board-ID matches the Spark bootloader build." \
    || say "STOP: Board-ID differs. Do not install the Spark bootloader; report INFO_UF2.TXT."
  pause "Press reset once so the app starts again"
  result
fi

if step H2 "Install the OTA-safe bootloader over USB (one time)"; then
  say "Risk: a bootloader update that is interrupted (cable, power) can brick the node; recovery is SWD."
  say "Keep USB connected and the batteries in. The copy takes seconds; do not touch the node."
  say "Room posts stored on the node may be lost (the update uses part of the post store as scratch)."
  [ -f "$BL_UF2" ] || { say "missing $BL_UF2 (P0)"; result; exit 1; }
  run shasum -a 256 "$BL_UF2"
  cli "get spark"
  if ask "Install $BL_UF2 now?"; then
    d="$(wait_drive)" || { say "no XIAO-BOOT drive"; result; exit 1; }
    grep -q "Board-ID: nRF52840-SeeedXiao-v1" "$d/INFO_UF2.TXT" || { say "Board-ID mismatch: not copying"; result; exit 1; }
    run cp "$BL_UF2" "$d/"; sync
    say "Wait about 10 s: the drive disappears, the new bootloader starts the app."
    sleep 10
    say "Check: double-press again, INFO_UF2.TXT must show UF2 Bootloader 0.11.0-spark1."
    d="$(wait_drive)" && run cat "$d/INFO_UF2.TXT"
    pause "Press reset once so the app starts again"
    cli "get spark"
    say "If the post count looks wrong or posts are garbled: node_cli.py --port $PORT 'posts reset'"
  fi
  result
fi

if step H3 "Flash SparkNode1.1 over USB"; then
  if ask "Copy $OUT/SparkNode1.1.uf2 to the node?"; then
    d="$(wait_drive)" || { say "no XIAO-BOOT drive"; result; exit 1; }
    run cp "$OUT/SparkNode1.1.uf2" "$d/"; sync
    sleep 12
  fi
  cli "ver"; cli "get build"
  say "expected: $(zip_build "$OUT/SparkNode1.1.zip")"
  cli "get spark"
  say "Start the bridge again and check that it reconnects without re-pairing (log: 'connected to')."
  result
fi

if step T1 "Passive scan: the app advertises (NUS)"; then
  stop_bridge
  run "$BRIDGE" scan --seconds 20
  say "Note what the LEDs do in app mode."
  result
fi

if step T2 "dfu over USB: bootloader at MAC+1, unbonded; reset returns to the app"; then
  cli "dfu"
  sleep 3
  run "$BRIDGE" scan --seconds 20
  say "Expected: a 'bootloader' row (AdaDFU) at the address in the dfu reply. Note the LEDs."
  pause "Press reset once"
  sleep 8
  cli "get build"; cli "get spark"
  say "Expected: same build, same post count, identity unchanged (get public.key)."
  result
fi

if step T4 "spark-bridge update, 5 runs (same build with --force, then a new build)"; then
  say "Start the bridge service again first: the updater must pause it and it must reconnect after."
  pause "Bridge running?"
  for i in 1 2 3 4 5; do
    case "$i" in   # 1: the running build again (needs --force), then alternate new builds
      1) z="$OUT/SparkNode1.1.zip"; f="--force" ;;
      2|4) z="$OUT/SparkNode1.1b.zip"; f="" ;;
      *) z="$OUT/SparkNode1.1.zip"; f="" ;;
    esac
    ask "Run $i: update to $(basename "$z") $f?" || continue
    run "$BRIDGE" update "$z" --yes $f; say "exit code $?"
    cli "get build"; cli "get spark"
  done
  say "Pass: 5 of 5 verified, no Bluetooth toggle, room posts and ACL intact, bridge reconnected."
  result
fi

if step T5 "Abort before Start: node waits in the bootloader; the tool finishes it"; then
  stop_bridge
  cli "dfu"; sleep 60
  run "$BRIDGE" scan --seconds 15
  say "Expected: still advertising as bootloader after 60 s (no timeout)."
  run "$BRIDGE" update "$OUT/SparkNode1.1b.zip" --in-bootloader --yes; say "exit code $?"
  cli "get build"
  say "Second part: dfu again, then just press reset: the old app must come back."
  cli "dfu"; sleep 5; pause "Press reset once"; sleep 8; cli "get build"
  result
fi

if step T6 "Abort after Start (node on the desk)"; then
  trap ':' INT   # Ctrl-C stops the updater, not this script
  say "a) Start an update and press Ctrl-C at about 30 %. The tool says the node has no app."
  pause "Ready?"
  run "$BRIDGE" update "$OUT/SparkNode1.1.zip" --yes; say "exit code $?"
  run "$BRIDGE" scan --seconds 15
  say "   Expected (Spark bootloader): a bootloader row. Now retry:"
  run "$BRIDGE" update "$OUT/SparkNode1.1.zip" --in-bootloader --yes; say "exit code $?"
  cli "get build"
  say "b) Again Ctrl-C at about 30 %, then power-cycle the node (USB out, battery switch off/on)."
  pause "Ready?"
  run "$BRIDGE" update "$OUT/SparkNode1.1b.zip" --yes
  pause "Power-cycle the node now"
  run "$BRIDGE" scan --seconds 20
  say "   Expected: bootloader advertising after power-on (no app -> BLE OTA, not USB)."
  run "$BRIDGE" update "$OUT/SparkNode1.1b.zip" --in-bootloader --yes; say "exit code $?"
  cli "get build"
  say "c) Again Ctrl-C at about 30 %, then recover over USB: double-press must still give XIAO-BOOT."
  pause "Ready?"
  run "$BRIDGE" update "$OUT/SparkNode1.1.zip" --yes
  d="$(wait_drive)" && run cp "$OUT/SparkNode1.1.uf2" "$d/" && sync
  sleep 12; cli "get build"; cli "get spark"
  trap - INT
  result
fi

if step T7 "Service Changed: bonded centrals re-discover by themselves"; then
  say "After the updates above the bridge (bonded Mac and/or Pi) must have reconnected without a"
  say "Bluetooth toggle or re-pairing. Check its log for 'connected to ... (v1.18.0-SparkNode1.1)'."
  say "A full test needs an image with a different GATT layout (e.g. a build without the BLEDfu"
  say "service); flash it with spark-bridge update and check the bridge reconnects; then go back."
  result
fi

if step T8 "Pi at its final location: 5 updates, RSSI logged"; then
  say "On the Pi: host/.venv/bin/spark-bridge scan (note the RSSI of the bootloader row),"
  say "then 5 x host/.venv/bin/spark-bridge update <zip> --yes, alternating SparkNode1.1/SparkNode1.1b."
  result
fi

if step T9 "Phone fallback (nRF Connect or nRF DFU app)"; then
  cli "dfu"
  say "On the phone: DFU, pick the AdaDFU device, choose $OUT/SparkNode1.1.zip (copy it to the phone)."
  pause "Done on the phone?"
  sleep 10; cli "get build"
  result
fi

say "Log: $LOG"
