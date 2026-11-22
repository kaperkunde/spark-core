#!/usr/bin/env bash
# Set the node's BLE PIN over USB serial without the PIN touching shell history or the process list.
# Usage: tools/set-ble-pin.sh /dev/cu.usbmodemXXXX   (or /dev/ttyACM0)
# The PIN is read silently, must be 6 digits, and is written as "set ble.pin <pin>\r".
# The bridge itself never sees or stores the PIN, and refuses "set ble.pin" over the air.
set -euo pipefail

PORT="${1:?usage: $0 <serial-port>}"
[ -c "$PORT" ] || { echo "$PORT is not a character device" >&2; exit 1; }
read -r -s -p "New 6-digit BLE PIN: " PIN; echo
read -r -s -p "Again: " PIN2; echo
[ "$PIN" = "$PIN2" ] || { echo "PINs differ" >&2; exit 1; }
[[ "$PIN" =~ ^[0-9]{6}$ ]] || { echo "PIN must be exactly 6 digits" >&2; exit 1; }
stty -F "$PORT" 115200 raw -echo 2>/dev/null || stty -f "$PORT" 115200 raw -echo
printf 'set ble.pin %s\r' "$PIN" > "$PORT"
unset PIN PIN2
echo "sent. Re-pair the bridge host with the node at OS level (the old bond is invalid)."
