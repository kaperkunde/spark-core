#!/usr/bin/env bash
# Build the OTA-safe UF2 bootloader for the SenseCAP Solar Node P1 (SparkNode).
#
#   spark/bootloader/build.sh            -> out/spark/bootloader/update-*.uf2 (and .hex)
#
# What it builds: Adafruit_nRF52_Bootloader 0.11.0 for the Seeed XIAO nRF52840 board (the P1's
# module; same USB VID/PID, UF2 board id "nRF52840-SeeedXiao-v1" and volume XIAO-BOOT), for
# SoftDevice S140 7.3.0 (the one already on the node), with DEFAULT_TO_OTA_DFU and
# 0001-ota-dfu-only-without-app.patch: when the application is missing (a BLE transfer broke
# off after the old app was erased), the bootloader waits for BLE OTA instead of USB, so a
# rooftop node can be retried from the ground. Double reset still opens the XIAO-BOOT drive.
#
# Install it once over USB (spark/README.md, "OTA-safe bootloader"): copy update-*.uf2 to the
# XIAO-BOOT drive. Never build or flash it without reading that section.
#
# Environment: SPARK_BOOTLOADER_REPO (git URL or local clone, default GitHub), ARM_GCC_BIN
# (directory with arm-none-eabi-gcc; default: PlatformIO's toolchain).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/spark/bootloader"
TOOLS="$ROOT/.spark-tools"
SRC="$TOOLS/Adafruit_nRF52_Bootloader"
OUT="$ROOT/out/spark/bootloader"

REPO="${SPARK_BOOTLOADER_REPO:-https://github.com/adafruit/Adafruit_nRF52_Bootloader.git}"
TAG="0.11.0"
COMMIT="c67f0bcf0fa8e841426335b1bbde91cda6ca1f50"   # tag 0.11.0
SPARK_TAG="$TAG-spark1"                            # shows as "UF2 Bootloader 0.11.0-spark1"
BOARD="xiao_nrf52840_ble"
SD_VERSION="7.3.0"

ARM_GCC_BIN="${ARM_GCC_BIN:-$HOME/.platformio/packages/toolchain-gccarmnoneeabi/bin}"
[ -x "$ARM_GCC_BIN/arm-none-eabi-gcc" ] || command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
  echo "arm-none-eabi-gcc not found: build SparkNode once with PlatformIO, or set ARM_GCC_BIN" >&2; exit 1; }
export PATH="$ARM_GCC_BIN:$PATH"

# source: pinned commit + the Spark patch, committed and tagged locally so that git describe
# (which the Makefile uses for the version strings) says 0.11.0-spark1
if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$TOOLS"
  git clone --quiet "$REPO" "$SRC"
fi
git -C "$SRC" fetch --quiet --tags "$REPO" 2>/dev/null || true
git -C "$SRC" checkout --quiet --force "$COMMIT"
git -C "$SRC" submodule update --quiet --init --recursive
git -C "$SRC" -c user.name=spark -c user.email=spark@invalid -c commit.gpgsign=false \
  am --quiet --keep-cr --committer-date-is-author-date "$HERE/0001-ota-dfu-only-without-app.patch"
git -C "$SRC" tag -f "$SPARK_TAG" >/dev/null

# python with intelhex for the Makefile's hex tools
VENV="$TOOLS/bootloader-venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet intelhex
fi

make -C "$SRC" BOARD="$BOARD" _VER3="0 11 0" clean >/dev/null
# _VER3: the Makefile derives it with a GNU-only sed expression; set it for BSD sed (macOS)
make -C "$SRC" BOARD="$BOARD" SD_VERSION="$SD_VERSION" DEFAULT_TO_OTA_DFU=1 OTA_DFU_ONLY_WITHOUT_APP=1 \
  PYTHON="$VENV/bin/python" _VER3="0 11 0" \
  "_build/build-$BOARD/${BOARD}_bootloader-${SPARK_TAG}_nosd.hex" \
  "_build/build-$BOARD/update-${BOARD}_bootloader-${SPARK_TAG}_nosd.uf2"

B="$SRC/_build/build-$BOARD"
mkdir -p "$OUT"
cp "$B/update-${BOARD}_bootloader-${SPARK_TAG}_nosd.uf2" "$B/${BOARD}_bootloader-${SPARK_TAG}_nosd.hex" "$OUT/"

# sanity checks on what was built
UF2="$OUT/update-${BOARD}_bootloader-${SPARK_TAG}_nosd.uf2"
STRS="$(strings -a "$B/${BOARD}_bootloader-${SPARK_TAG}.out")"
grep -q "$SPARK_TAG s140 $SD_VERSION" <<<"$STRS" \
  || { echo "built bootloader does not report s140 $SD_VERSION" >&2; exit 1; }
grep -q "Board-ID: nRF52840-SeeedXiao-v1" <<<"$STRS" \
  || { echo "built bootloader has an unexpected UF2 board id" >&2; exit 1; }
FLAGS="$(make -C "$SRC" BOARD="$BOARD" SD_VERSION="$SD_VERSION" DEFAULT_TO_OTA_DFU=1 OTA_DFU_ONLY_WITHOUT_APP=1 \
  _VER3="0 11 0" print-CFLAGS)"
grep -q -- "-DDEFAULT_TO_OTA_DFU -DOTA_DFU_ONLY_WITHOUT_APP" <<<"$FLAGS" \
  || { echo "OTA flags missing from CFLAGS" >&2; exit 1; }
echo "Built $UF2 ($(wc -c < "$UF2" | tr -d ' ') bytes)"
(cd "$OUT" && shasum -a 256 "$(basename "$UF2")" 2>/dev/null || sha256sum "$(basename "$UF2")")
