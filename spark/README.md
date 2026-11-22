# Spark guide

Spark is a set of three parts built on [MeshCore](https://github.com/meshcore-dev/MeshCore) v1.18.
It turns one solar roof node into a hub for your household and your neighbourhood.

| Part | Version | What it is |
|---|---|---|
| **SparkNode** | SparkNode1 (`v1.18.0-SparkNode1`) | Firmware for a SenseCAP Solar Node P1 on your roof: a MeshCore **room** with store-and-forward for your own people, and a **relay** that strengthens the local mesh. |
| **Spark Bridge** | SparkBridge1 (`spark-bridge 1`) | A small Python service on a Raspberry Pi or computer near the node. It links the node to **Discord**, one-way or two-way, with MeshCore Public and with the SparkNode room. |
| **SparkKids** | SparkKids1 | Firmware for a **LilyGO T-Deck Plus**: a simple touch messenger with location sharing for kids, or anyone who doesn't like fancy tech. A "dumb phone" without a service plan. |

```
 T-Deck Plus (SparkKids) ─┐
 phones (MeshCore app) ───┼─ LoRa ─ SparkNode (roof) ─ BLE ─ Spark Bridge (Pi/Mac) ─ Internet ─ Discord
 other MeshCore nodes ────┘            room + relay
```

The node keeps working on its own. When the bridge, Bluetooth or the internet is down, the room
still stores and forwards messages and the relay keeps relaying. Only the Discord link pauses.

Contents: [What you need](#what-you-need) · [Install with setup.sh](#install-with-setupsh) ·
[Configuration (.env)](#configuration-env) · [SparkNode](#sparknode) · [Spark Bridge](#spark-bridge) ·
[SparkKids](#sparkkids) · [Building by hand](#building-by-hand) · [For developers](#for-developers)

## What you need

- **A roof node.** The [SenseCAP Solar Node P1](https://www.seeedstudio.com/SenseCAP-Solar-Node-P1-for-Meshtastic-LoRa-p-6425.html)
  from Seeed Studio (solar panel; batteries and GPS sold separately), or the
  [SenseCAP Solar Node P1-Pro](https://www.seeedstudio.com/SenseCAP-Solar-Node-P1-Pro-for-Meshtastic-LoRa-p-6412.html)
  with batteries and GPS included. Seeed sells them "for Meshtastic", but they run MeshCore too.
  Other MeshCore-capable boards can work as well: SparkNode is the standard MeshCore room
  server plus a few build flags, so another board needs its own PlatformIO env modelled on
  `variants/spark_p1/platformio.ini`. Only the P1 is tested.
- **A bridge host** within Bluetooth range of the node (or connected to it by USB): a Raspberry
  Pi with Raspberry Pi OS (Bookworm), or a Mac. Only needed for Discord.
- **For SparkKids:** a [LilyGO T-Deck Plus](https://www.lilygo.cc/products/t-deck-plus) per person.
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
3. **Flashes SparkNode:** you double-press the node's reset button, a UF2 drive (`XIAO-BOOT`)
   appears, and setup.sh copies the firmware to it. It can then read the node's public key over
   USB, which SparkKids needs.
4. **Flashes SparkKids:** you put the T-Deck in download mode (off, hold the trackball, on).
   Before the first flash it offers a full 16 MB backup of the device, read twice and compared
   by SHA-256, into `backups/`. It then writes the factory image (first time) or the app
   (updates) with esptool `--after watchdog-reset`. It never erases the device.
5. **Installs Spark Bridge:** a Python venv in `host/.venv` and a service: systemd on the Pi,
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
For SparkNode the name, passwords, radio, scope and path hash are the **defaults for a new
node**: a node that already has saved settings keeps them across a reflash. Change those on the
node itself (USB console, the MeshCore app's admin login, or `/spark cli` from Discord), for
example `set name`, `password`, `set guest.password`, `set radio`, the `region` commands.

## SparkNode

SparkNode is the stock MeshCore room server with these additions, each behind a build flag
(all on in the `SparkNode_P1` env):

| Flag | Feature |
|---|---|
| `WITH_HOSTLINK` | HostLink: a subset of the companion frame protocol over USB serial and BLE for the bridge; Public channel receive/send, advert name cache, TRACE path ping |
| `HOSTLINK_BLE` | BLE transport (Nordic UART, passkey pairing) |
| `ROOM_PERSIST_POSTS` | room posts kept in their own flash region across reboots; clock floor after reboot |
| `ROOM_ALLOW_REPEAT_TOGGLE` | `set repeat on` works again, so the room is also a relay |
| `ROOM_MEMBER_DIR` | member directory (names, parents) and SparkKids locations (`members`, `where`) |

With none of the flags the room server builds as stock. The protocol is in [HOSTLINK.md](HOSTLINK.md).

**Relay.** Turn relaying on once on the node: `set repeat on`.

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

**Version.** `ver` prints `v1.18.0-SparkNode1`.

## Spark Bridge

See [host/README.md](../host/README.md) for the Discord setup, the per-channel `/spark` commands
and testing without hardware. In short: create a dedicated Discord bot, put its token and your
server id in `.env`, pair the node once over Bluetooth, start the service, then in a channel run
`/spark receive both` (read-only mirror) or `/spark send room` (two-way with the room).

## SparkKids

A one-room messenger for the LilyGO T-Deck Plus, made for children and anyone who wants
something simple: big touch targets, no menus to get lost in, no phone plan.

- **Room:** the home screen is your SparkNode room. Everyone in your tribe reads and writes here;
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
pio run -e SparkNode_P1 -t create_uf2                  # .pio/build/SparkNode_P1/firmware.uf2
pio run -e SparkKids_TDeckPlus -t mergebin             # firmware-merged.bin (0x0) and firmware.bin (0x10000)
pio test -e native_spark                               # Spark unit tests
pio test -e native                                     # upstream unit tests
cd host && python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest
```

`SPARK_ENV_FILE=/path/to/file` builds with another file than `.env`.

Flashing by hand: SparkNode: double-press reset, copy `firmware.uf2` to the UF2 drive. SparkKids
(download mode): `esptool --chip esp32s3 -p <port> -b 921600 --after watchdog-reset write-flash 0x0 firmware-merged.bin`
the first time, `write-flash 0x10000 firmware.bin` for updates. `--after watchdog-reset` matters:
with the default reset the ESP32-S3 stays in its ROM bootloader.

## For developers

- [HOSTLINK.md](HOSTLINK.md): the node to bridge protocol.
- [UPSTREAM.md](UPSTREAM.md): how this fork tracks MeshCore, the hooks in upstream files, versions.
- Code: `examples/spark_room/` (node additions), `examples/spark_kids/` (SparkKids),
  `variants/spark_p1/`, `variants/spark_tdeck/`, `host/` (bridge), `spark/pio_env.py` (.env to
  build flags), `spark/tools/`, `setup.sh`.
