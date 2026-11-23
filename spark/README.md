# SpArk guide

SpArk is a set of three parts built on [MeshCore](https://github.com/meshcore-dev/MeshCore) v1.18.
It turns one solar roof node into a hub for your household and your neighbourhood.

| Part | Version | What it is |
|---|---|---|
| **SpArkNode** | SpArkNode1.3 (`v1.18.0-SpArkNode1.3`) | Firmware for a SenseCAP Solar Node P1 on your roof: a MeshCore **room** with store-and-forward for your own people, and a **relay** that strengthens the local mesh. |
| **SpArk Room Bridge** | SpArkBridge1.3 (`spark-bridge 1.3`) | A small Python service on a Raspberry Pi or computer near the node. It links the node to **Discord**, one-way or two-way, with MeshCore Public and with the SpArkNode room, and can make the node an **MQTT observer** for MC-Radar / Let's Mesh. |
| **SpArkKids** | SpArkKids1 | Firmware for a **LilyGO T-Deck Plus**: a simple touch messenger with location sharing for kids, or anyone who doesn't like fancy tech. A "dumb phone" without a service plan. |

```
 T-Deck Plus (SpArkKids) ─┐
 phones (MeshCore app) ───┼─ LoRa ─ SpArkNode (roof) ─ BLE ─ SpArk Room Bridge (Pi/Mac) ─ Internet ─ Discord
 other MeshCore nodes ────┘            room + relay
```

The node keeps working on its own. When the bridge, Bluetooth or the internet is down, the room
still stores and forwards messages and the relay keeps relaying. Only the Discord link pauses.

Contents: [What you need](#what-you-need) · [Install with setup.sh](#install-with-setupsh) ·
[Configuration (.env)](#configuration-env) · [SpArkNode](#sparknode) · [Update over Bluetooth](#update-over-bluetooth) ·
[SpArk Room Bridge](#spark-bridge) ·
[SpArkKids](#sparkkids) · [Building by hand](#building-by-hand) · [For developers](#for-developers)

## What you need

- **A roof node.** The [SenseCAP Solar Node P1](https://www.seeedstudio.com/SenseCAP-Solar-Node-P1-for-Meshtastic-LoRa-p-6425.html)
  from Seeed Studio (solar panel; batteries and GPS sold separately), or the
  [SenseCAP Solar Node P1-Pro](https://www.seeedstudio.com/SenseCAP-Solar-Node-P1-Pro-for-Meshtastic-LoRa-p-6412.html)
  with batteries and GPS included. Seeed sells them "for Meshtastic", but they run MeshCore too.
  Other MeshCore-capable boards can work as well: SpArkNode is the standard MeshCore room
  server plus a few build flags, so another board needs its own PlatformIO env modelled on
  `variants/spark_p1/platformio.ini`. Only the P1 is tested.
- **A bridge host** within Bluetooth range of the node (or connected to it by USB): a Raspberry
  Pi with Raspberry Pi OS (Bookworm), or a Mac. Only needed for Discord.
- **For SpArkKids:** a [LilyGO T-Deck Plus](https://www.lilygo.cc/products/t-deck-plus) per person.
- A computer with USB to build and flash (macOS or Raspberry Pi OS). `setup.sh` installs
  PlatformIO and esptool when they are missing.

## Install with setup.sh

```sh
git clone https://github.com/kaperkunde/spark-core.git
cd spark-core
./setup.sh --dry-run      # optional: see every question and command, changes nothing
./setup.sh
```

`setup.sh` asks which parts to set up (any combination), then:

1. **Asks the configuration questions** for those parts and writes `.env`. Re-running it offers
   your current values. Radio preset **NL** (Dutch MeshCore: 869.618 MHz, BW 62.5, SF7, CR5,
   scope `nl`, 2-byte path hashes) is the default; **EU** (MeshCore EU/UK default) and
   **custom** are offered too. The time zone defaults to your system's. Passwords and PINs you
   leave empty are generated and shown once.
2. **Builds** the chosen firmware with PlatformIO (installed into `~/.platformio` if missing).
3. **Flashes SpArkNode:** you double-press the node's reset button, a UF2 drive (`XIAO-BOOT`)
   appears, and setup.sh copies the firmware to it. It can then read the node's public key over
   USB, which SpArkKids needs.
4. **Flashes SpArkKids:** you put the T-Deck in download mode (off, hold the trackball, on).
   Before the first flash it offers a full 16 MB backup of the device, read twice and compared
   by SHA-256, into `backups/`. It then writes the factory image (first time) or the app
   (updates) with esptool `--after watchdog-reset`. It never erases the device.
5. **Installs SpArk Room Bridge:** a Python venv in `host/.venv` and a service: systemd on the Pi,
   a launchd agent on the Mac.

Options: `--dry-run` (writes `.env.dry-run`, prints commands instead of running them),
`--defaults` (accept every default; handy with `--dry-run`), `--env FILE`.

## Configuration (.env)

Everything is configured in one file, `.env` at the top of the repository. It is gitignored and
holds secrets (passwords, PINs, the Discord token): never commit it. [`.env.example`](../.env.example)
documents every key.

| Keys | Used by | Meaning |
|---|---|---|
| `SPARK_RADIO_PRESET`, `SPARK_LORA_FREQ`, `_BW`, `_SF`, `_CR`, `_TX_POWER` | node, kids | radio settings |
| `SPARK_SCOPE`, `SPARK_PATH_HASH_SIZE` | node, kids | flood scope (region) and bytes per hop |
| `SPARK_TZ`, `BRIDGE_TZ` | kids, bridge | time zone: POSIX TZ string for the firmware, IANA name for the bridge (setup.sh asks once, writes both) |
| `SPARK_NODE_NAME`, `SPARK_NODE_LAT`, `SPARK_NODE_LON` | node (bridge, kids) | name and advertised position |
| `SPARK_ADMIN_PASSWORD`, `SPARK_ROOM_PASSWORD` | node (kids) | admin and room (guest) passwords |
| `SPARK_BLE_PIN` | node | BLE PIN for the bridge link (empty = BLE off) |
| `KIDS_ROOM_KEY`, `KIDS_ROOM_NAME`, `KIDS_ROOM_PASSWORD` | kids | which room to join |
| `KIDS_PARENT_PIN` | kids | parent PIN |
| `KIDS_PUBLIC_ENABLED`, `KIDS_PUBLIC_PER_HOUR` | kids | Public channel default and post limit |
| `DISCORD_TOKEN`, `BRIDGE_GUILD_IDS` | bridge | bot token and the servers it may act in |
| `BRIDGE_TRANSPORT`, `BRIDGE_BLE_NAME`, `BRIDGE_BLE_ADDRESS`, `BRIDGE_SERIAL_PORT`, `BRIDGE_TCP_*` | bridge | how it reaches the node |
| `BRIDGE_WEBHOOK_NAME`, `BRIDGE_DEFAULT_PER_MINUTE`, `BRIDGE_PUBLIC_PER_MINUTE`, `BRIDGE_PUBLIC_BURST` | bridge | Discord behaviour, channel defaults and limits |
| `BRIDGE_HEALTH_*`, `BRIDGE_DB_PATH`, `BRIDGE_ALLOW_DANGEROUS_CLI`, link supervision keys | bridge | health report, state, safety |

Firmware reads `.env` at build time (`spark/pio_env.py`), so rebuild and reflash after a change.
For SpArkNode the name, passwords, radio, scope and path hash are the **defaults for a new
node**: a node that already has saved settings keeps them across a reflash. Change those on the
node itself (USB console, the MeshCore app's admin login, or `/spark cli` from Discord), for
example `set name`, `password`, `set guest.password`, `set radio`, the `region` commands.

## SpArkNode

SpArkNode is the stock MeshCore room server with these additions, each behind a build flag
(all on in the `SparkNode_P1` env):

| Flag | Feature |
|---|---|
| `WITH_HOSTLINK` | HostLink: a subset of the companion frame protocol over USB serial and BLE for the bridge; Public channel receive/send, advert name cache, TRACE path ping |
| `HOSTLINK_BLE` | BLE transport (Nordic UART, passkey pairing) |
| `ROOM_PERSIST_POSTS` | room posts kept in their own flash region across reboots; clock floor after reboot |
| `ROOM_ALLOW_REPEAT_TOGGLE` | `set repeat on` works again, so the room is also a relay |
| `ROOM_MEMBER_DIR` | member directory (names, parents) and SpArkKids locations (`members`, `where`) |

With none of the flags the room server builds as stock. The protocol is in [HOSTLINK.md](HOSTLINK.md).

**Relay.** Turn relaying on once on the node: `set repeat on`. From SpArkNode1.2 the relay applies
the same filters as a MeshCore repeater: a flood whose region is denied (`region denyf ...`; with
`region denyf *` un-scoped floods are not relayed) and, with `set loop.detect minimal|moderate|strict`,
a flood that already passed this node too often are not forwarded. Every flood scope the relay should
carry must be in the node's region list and allowed (e.g. the kids' and the room's scope). This only
affects forwarding: the room still receives and answers everything, and its own posts go out.

**Memory layout.** The env uses the `nrf52840_s140_v7_extrafs.ld` linker script: the app may use
up to 708,608 bytes and the 100 KB region `0xD4000..0xED000` holds the post store. InternalFS
(identity, prefs, ACL, `/spark_hl`, `/members`) stays at `0xED000`, so an existing node keeps its
key and settings across the reflash. Reflashing a stock UF2 is harmless.

**BLE PIN.** BLE is off until there is a PIN: `SPARK_BLE_PIN` from `.env`, or one set on the USB
console, which always wins:

```
set ble.pin <6 digits, first digit not 0>      (USB serial only; then reboot)
get ble.pin                                    (prints set / unset, never the value)
set ble off                                    (kill switch, also usable via LoRa admin)
ble unpair                                     (clear bonds)
```

`host/tools/set-ble-pin.sh <port>` sets it without the PIN touching your shell history. BLE starts
30 s after boot (starting it while USB enumerates was seen to kill USB serial); `set ble off` over
USB within those 30 s keeps it off. Pair the bridge host once at OS level; the PIN is never sent
in a frame and never stored by the bridge.

**Parents' commands** (USB, LoRa admin login, or `/spark cli`): `members`, `member del <prefix|name>`,
`member name <prefix> <name>`, `where`, `where <name>`.

**Version.** `ver` prints `v1.18.0-SpArkNode1.3 (Build: <date>) <build id>`; `get build` prints the version and build id.
The version field is limited to 20 bytes, so a bug-fix release keeps the label `v1.18.0-SpArkNode1.3`
and is built with `-D SPARK_RELEASE_PATCH=1` (shown as `(patch 1)` by `ver` and `get spark`, i.e.
SpArkNode1.3.1); the build id already tells builds apart. Contract changes get a new minor version.

**Observer feed (SpArkNode1.3).** The node can hand every packet it receives to the bridge (HostLink
LOG_RX_DATA, off unless the bridge asks for it per session), and signs the bridge's MQTT login token
with its own key, so the station can be a [DutchMeshCore / MC-Radar observer](https://meshwiki.nl/wiki/Observer)
without WiFi and without exporting its key. `get spark` shows `rxlog:<sent>/<dropped>`.

## Update over Bluetooth

From SpArkNode1.1 on, the roof node can be updated over Bluetooth from the bridge host. The
first SpArkNode1.1 goes on over USB (SpArkNode1 has no `dfu` command).

```sh
pio run -e SparkNode_P1 -t dfu_zip                       # .pio/build/SparkNode_P1/firmware-dfu.zip
host/.venv/bin/spark-bridge update .pio/build/SparkNode_P1/firmware-dfu.zip
```

or `./setup.sh` and answer `ble` to "Install SpArkNode". `spark-bridge update` pauses a running
bridge, checks the package, the node's build and its battery (at least 3.7 V), sends `dfu`,
transfers to the bootloader, checks the new build id and lets the bridge reconnect. Details and
exit codes: [host/README.md](../host/README.md#updating-the-node-over-bluetooth-spark-bridge-update).
`spark-bridge scan` lists what advertises (app or bootloader); a node that is connected to a
host does not advertise.

**How it works.** `dfu` (USB console or the bridge link, never LoRa admin; `dfu force` skips
the battery check) replies with the bootloader's Bluetooth address, closes the link, drops the
bond hand-over data and resets into the bootloader's Bluetooth mode. Without a bond the
bootloader advertises openly as `AdaDFU` at the node's address + 1 (last byte), so macOS and
BlueZ see a new, unbonded device and no stale GATT cache gets in the way. Until the update is
done, anyone in Bluetooth range could send it firmware; the window is the few minutes of the
update. SpArkNode1.1 also sends a Service Changed indication on the first secured connection after
each boot, so bonded hosts re-read the GATT table after an update.

**Node commands.** `dfu [force]`, `get build` (`> v1.18.0-SpArkNode1.3 <git hash>[+]-<UTC time>`;
`+` = built from uncommitted changes), `ver` (with the build id), `posts reset` (USB only:
erase all stored room posts).

### OTA-safe bootloader

The SenseCAP P1 ships with a single-bank Adafruit UF2 bootloader (Seeed XIAO nRF52840 build).
Once a transfer has started, the old app is erased. If the transfer then breaks off (radio, power,
host), the stock bootloader comes back after a reset in **USB-only** mode: on a roof, that means
a ladder.

`spark/bootloader/build.sh` builds Adafruit_nRF52_Bootloader **0.11.0** for the same board
(`xiao_nrf52840_ble`, UF2 Board-ID `nRF52840-SeeedXiao-v1`, USB 2886:0044, volume XIAO-BOOT),
for the same SoftDevice (**S140 7.3.0**) and flash layout, with `DEFAULT_TO_OTA_DFU` and a small
patch (`0001-ota-dfu-only-without-app.patch`). With it:

- no valid app (a transfer broke off, also after a power cycle): the bootloader waits for a
  Bluetooth update (`spark-bridge update --in-bootloader <zip>`, or a phone) instead of USB;
- a Reset in the middle of a transfer restarts the Bluetooth session, so the updater retries by
  itself (3 attempts);
- double reset still opens the XIAO-BOOT drive, and the app's own UF2/serial DFU requests still
  go to USB. (Upstream's `DEFAULT_TO_OTA_DFU` alone also sends those to Bluetooth, which would
  leave no way to reach the UF2 drive; the patch narrows it to "no valid app".)

It shows as `UF2 Bootloader 0.11.0-spark1` in INFO_UF2.TXT and `0.11.0-spark1 s140 7.3.0` in
the bootloader's Bluetooth device information.

**Install it once, over USB, with the node on the desk** (`./setup.sh` offers it, default no):

1. `spark/bootloader/build.sh` -> `out/spark/bootloader/update-xiao_nrf52840_ble_bootloader-0.11.0-spark1_nosd.uf2`.
2. Batteries in, USB connected, double-press reset. Open `INFO_UF2.TXT` on XIAO-BOOT: it must
   say `Board-ID: nRF52840-SeeedXiao-v1`. Anything else: stop, this build is not for that board.
3. Copy the `update-...uf2` file to the drive and leave the node alone for 30 s. The running
   bootloader checks the board id, stages the new one and copies it into place; then the app starts.
4. Double-press again: INFO_UF2.TXT now shows `0.11.0-spark1`. Press reset once.

**Risks.** Writing a bootloader is the one step that can brick the node: if power or USB is lost
while the new bootloader is copied into place (about a second), nothing boots and only an SWD
programmer (J-Link, Raspberry Pi with OpenOCD, or a CMSIS-DAP probe on the nRF52840 module's SWD pads: SWDIO, SWCLK, GND) can
write `xiao_nrf52840_ble_bootloader-0.11.0-spark1_nosd.hex` (plus the S140 7.3.0 SoftDevice
if it was hit) back. The application, InternalFS (identity, settings, ACL) and the board id are
kept. The staging copy uses 40 KB below 0xEA000, which lies inside the room post store: stored
room posts may be lost or damaged; check `get spark` afterwards and run `posts reset` if needed.
An app that is valid but hangs at boot is not caught by any bootloader setting: test every
image on the desk before sending it to the roof.

### Recovery

| Situation | What to do |
|---|---|
| `spark-bridge update` exit 1 (before Start) | The old app is intact. If the node is still in the bootloader (`spark-bridge scan`), press reset or run the update again. |
| exit 4, SpArk bootloader | The node waits for Bluetooth (also after a power cycle): `spark-bridge update --in-bootloader <zip>`, or nRF Connect / nRF DFU app on a phone with the zip. |
| exit 4, stock bootloader | The bootloader session cannot take a new Start; a reset gives USB mode. Recovery is USB: double-press reset, copy the UF2. |
| Node in USB mode (XIAO-BOOT) | Copy `firmware.uf2` (SpArkNode1.1, or the previous release) to the drive. |
| Nothing works (no drive, no advertising) | SWD: write the bootloader hex, then the app UF2 over USB. |

Keep the UF2 of the running release and its DFU zip on the bridge host before every update.
The full bench test is [OTA-TESTS.md](OTA-TESTS.md) (`spark/tools/ota_hwtest.sh`).

## SpArk Room Bridge

See [host/README.md](../host/README.md) for the Discord setup, the per-channel `/spark` commands
and testing without hardware. In short: create a dedicated Discord bot, put its token and your
server id in `.env`, pair the node once over Bluetooth, start the service, then in a channel run
`/spark receive both` (read-only mirror) or `/spark send room` (two-way with the room).
Discord users post to the mesh under an alias (`/spark alias set`) or their display name; on
Public a SpArkNode1.2 node sends it as the sender name (`BadPirate: Hallo`). Enabling
`/spark send public` (or `both`) asks for a confirmation after a warning: anyone in that Discord
channel can then send to Public as your station. Public messages carry a small footer with hops,
SNR and (SpArkNode1.3) the route through named repeaters (`/spark footer on|off`).

**MQTT observer.** With `BRIDGE_OBSERVER=1`, a region code (`BRIDGE_OBSERVER_IATA=AMS`) and the
`dutchmeshcore` broker preset, the bridge publishes every packet the node hears to the DutchMeshCore
collectors that feed MC-Radar (or Let's Mesh). No account is needed: the node signs the login
token with its own key. `./setup.sh` asks for it; details in [host/README.md](../host/README.md).

## SpArkKids

A one-room messenger for the LilyGO T-Deck Plus, made for children and anyone who wants
something simple: big touch targets, no menus to get lost in, no phone plan.

- **Room:** the home screen is your SpArkNode room. Everyone in your tribe reads and writes here;
  the node stores messages while a device is off and pushes them when it is back.
- **People:** the room's members (parents first). Tap a person for a private message (DM).
- **Public (optional, off by default):** a "Public" row under the room in People opens MeshCore
  Public, the channel everyone nearby can read. Same radio, scope and path hash size as the room.
  Posts are rate-limited (`KIDS_PUBLIC_PER_HOUR`, at least 30 s apart); incoming Public posts
  are quiet (no sound, no screen wake-up) and kept only in memory, so they never push family
  messages out. When off, the device neither shows nor sends Public. Turn it on with
  `KIDS_PUBLIC_ENABLED=1` (the default for the device until a parent chooses) or on the device:
  People > gear > Parent settings (PIN) > **Public channel**. The parent's choice is saved.
- **Location:** with a GPS fix, the device sends its position to the room at most every 5 minutes
  while moving and every 15 minutes while still. Only parents see it (`where` on the node,
  `/spark where` in Discord). Parents can switch it off.
- **Controls:** touch first. Left swipe or `<` = back. The keyboard types into the message line;
  Enter sends. The trackball works as a fallback.

**First use.** The device asks "What is your name?", logs in to the room with the room password,
adverts its name and appears in the room's member directory. Parent settings: People > gear >
Parent settings, with `KIDS_PARENT_PIN`. There you can change the name, room password, room key,
PIN, volume, location sharing, trackball, touch calibration and the Public channel.

**USB console** (115200 baud): `info`, then `unlock <pin>` for `name`, `pw`, `key <64 hex>`, `pin`,
`share on|off`, `public [on|off]`, `relogin`, `advert`, `members`, `touchcal`, `time <epoch>`, `reboot`.

**Back up first, never erase.** A T-Deck may come with other firmware (and a licence tied to it).
`setup.sh` backs up the full 16 MB flash before the first flash. Restore it bit for bit with:

```
esptool --chip esp32s3 -p <port> -b 921600 write-flash 0x0 backups/tdeck-<mac>-<date>/full-1.bin
```

## Building by hand

```sh
cp .env.example .env                                   # and fill it in, or run ./setup.sh
pio run -e SparkNode_P1 -t create_uf2 -t dfu_zip      # .pio/build/SparkNode_P1/firmware.uf2 and firmware-dfu.zip
spark/bootloader/build.sh                              # OTA-safe bootloader update UF2 (see above)
pio run -e SparkKids_TDeckPlus -t mergebin             # firmware-merged.bin (0x0) and firmware.bin (0x10000)
pio test -e native_spark                               # SpArk unit tests
pio test -e native                                     # upstream unit tests
cd host && python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest
```

`SPARK_ENV_FILE=/path/to/file` builds with another file than `.env`.

Flashing by hand: SpArkNode: double-press reset, copy `firmware.uf2` to the UF2 drive. SpArkKids
(download mode): `esptool --chip esp32s3 -p <port> -b 921600 --after watchdog-reset write-flash 0x0 firmware-merged.bin`
the first time, `write-flash 0x10000 firmware.bin` for updates. `--after watchdog-reset` matters:
with the default reset the ESP32-S3 stays in its ROM bootloader.

## For developers

- [HOSTLINK.md](HOSTLINK.md): the node to bridge protocol.
- [UPSTREAM.md](UPSTREAM.md): how this fork tracks MeshCore, the hooks in upstream files, versions.
- Code: `examples/spark_room/` (node additions), `examples/spark_kids/` (SpArkKids),
  `variants/spark_p1/`, `variants/spark_tdeck/`, `host/` (bridge), `spark/pio_env.py` (.env to
  build flags), `spark/tools/`, `spark/bootloader/`, `setup.sh`.
- [OTA-TESTS.md](OTA-TESTS.md): bench and field tests for Bluetooth updates.
