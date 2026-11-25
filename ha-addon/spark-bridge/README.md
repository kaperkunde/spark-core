# SpArk Room Bridge: Home Assistant local add-on (UNTESTED skeleton)

Nothing here has been built or run. It follows the Home Assistant app documentation
(https://contributors.home-assistant.io/apps/reference/configuration/ and
https://contributors.home-assistant.io/apps/first-app/) and may need changes.

- `host_dbus: true` mounts the host's system D-Bus (`/run/dbus`, read-only) so bleak can reach BlueZ.
  The docs do not say whether that is enough for BLE inside the container; if not, try
  `privileged: [NET_ADMIN]` (and `udev: true`).
- `map: data` is `/data`, which survives updates. The bridge's `.env` is generated there from the
  options, and the SQLite state is `/data/spark.db`.
- `ble_adapter`: use the dedicated USB dongle (`hci1`, or its MAC, which survives reboots) so the
  onboard adapter stays with Home Assistant. Empty = the system default.

## Install (HA OS, local add-on)

1. Copy this folder to `/addons/spark-bridge` on the HA host (Samba/SSH `addons` share).
2. Copy the repository's `host/` folder into it as `host/` (the build context is this folder only).
3. Settings > Add-ons > Add-on Store > reload; install "SpArk Room Bridge" under Local add-ons.
4. Pair the node once with the dongle (`bluetoothctl` on the host, select the dongle controller),
   set the options, start.

Not done: `build.yaml`/multi-arch images, a Supervisor watchdog, ingress, translations, an icon.
