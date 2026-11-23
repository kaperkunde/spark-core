# Spark Bridge (SparkBridge1.3)

The Discord service of [Spark](../spark/README.md). It connects to a SparkNode (MeshCore room
server with the HostLink firmware) over BLE, USB serial or TCP (stub node) and bridges the mesh
to Discord.

```
mesh <-LoRa-> SparkNode <-BLE-> Spark Bridge <-network-> Discord
```

When the bridge or BLE is down the node keeps working. The bridge reconnects by itself (backoff
2, 4, 8 ... 60 s plus jitter, forever) while Discord stays connected. Python 3.11+, macOS and
Raspberry Pi OS (Bookworm). It uses its own dedicated Discord bot.

## Install

The easy way is `./setup.sh` at the top of the repository: it writes `.env`, creates the venv
and installs a systemd service (Pi) or launchd agent (Mac). By hand:

```sh
cp .env.example .env                  # at the repository root; fill in DISCORD_TOKEN, BRIDGE_GUILD_IDS, ...
cd host
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/spark-bridge                # finds ../.env; or: spark-bridge --env /path/to/.env
```

All settings come from the repository's single `.env` (keys `BRIDGE_*`, `DISCORD_TOKEN`, and
`SPARK_NODE_NAME` as the default BLE name); variables set in the process environment win.
`.env` is gitignored. There is no BLE PIN setting for the bridge: the PIN is never stored by it.
The older `--config config.toml` format still works.

### BLE pairing (once, at OS level)

- **macOS**: start `spark-bridge` in Terminal; macOS shows its own pairing dialog on the first
  connect. Enter the node's PIN. `BRIDGE_BLE_NAME` (default: the node name) matches the device
  name `MeshCore-<name>` (macOS hides MAC addresses). Give your terminal Bluetooth permission in
  System Settings > Privacy & Security; a launchd agent cannot ask for it.
- **Raspberry Pi**: `bluetoothctl`, then `scan on`, `pair <MAC>`, enter the PIN, `trust <MAC>`,
  `quit`. Put the MAC in `BRIDGE_BLE_ADDRESS` or rely on the name.
- To change the PIN: `tools/set-ble-pin.sh /dev/ttyACM0` (over USB; prompts silently). Then re-pair.

### Services

`setup.sh` renders the templates in `deploy/`: `spark-bridge.service` (systemd, runs as your user
with the `bluetooth` group, `journalctl -u spark-bridge -f`) and `com.kaperkunde.spark-bridge.plist`
(launchd, log in `~/Library/Logs/spark-bridge.log`). Both restart the bridge when it exits; it
exits with code 3 after `BRIDGE_GIVE_UP_MINUTES` without a link.

## Discord setup (once)

1. Developer Portal > New Application > Bot. Create a **new, dedicated** application for this bridge.
2. Bot tab: copy the token into `.env` as `DISCORD_TOKEN`. Under Privileged Gateway Intents enable **Message Content
   Intent**. Turn off "Public Bot" if you do not want others to add it.
3. OAuth2 > URL Generator: scopes `bot` and `applications.commands`. Bot permissions: View
   Channels, Send Messages, Read Message History, Add Reactions, Manage Webhooks (without it the bot
   falls back to `**name**: text` messages). Open the URL and add the bot to your server.
4. Put the server id (Developer Mode > right-click server > Copy Server ID) in
   `BRIDGE_GUILD_IDS`. The bridge ignores every other server; an empty list means none.
5. Slash commands are synced per allowed server at start-up and appear immediately.

## Channel behaviour (replaces fixed bind modes)

Every Discord channel has its own composable settings, stored in SQLite. Nothing happens in a
channel until it is configured. A user with **Manage Channels** in that channel runs the setting
commands. Which Discord users can use a channel is decided by Discord's channel permissions;
the bridge adds no user permission system.

| Command | Meaning |
|---|---|
| `/spark status` | show this channel's settings and the link state |
| `/spark admin on\|off` | allow `/spark cli`, `/spark health` and `/spark where` here. Turning admin on also turns `report` on the first time |
| `/spark send none\|public\|room\|both [confirm:True]` | Discord to mesh target. `public` and `both` first show a warning (below) and need **Confirm** (button) or `confirm:True` |
| `/spark prefix on\|off` | on: the sender is named (Public: `<alias>: text` as the sender name, room: `[alias] text`); off: plain text, sent as the node itself |
| `/spark alias set user:@user name:<mesh name>` | the name this Discord user has on the mesh (Manage Channels; no `:` or line breaks, at most 31 bytes, one user per name) |
| `/spark alias list` | all aliases |
| `/spark alias del user:@user` | remove an alias: the Discord display name is used again |
| `/spark ratelimit none\|channel\|user [per_minute]` | no limit, or N messages/minute per channel or per user (default 3) |
| `/spark receive none\|public\|room\|both` | mesh to Discord source copied into this channel |
| `/spark footer on\|off` | a small line under each Public message: `2 hops · SNR 5.0 dB · via NL-AMS-DAR1 → Bella Vista`, or `direct · SNR 5.0 dB` (default on; the route needs SparkNode1.3) |
| `/spark report on\|off` | the daily health line (`BRIDGE_HEALTH_REPORT`, default 15:05 in `BRIDGE_TZ`) and link up/down notices here |
| `/spark health` | health now, live from the node (admin channels) |
| `/spark cli command:<text>` | node CLI command (admin channels) |
| `/spark where [name]` | latest GPS fix of every room member (or one), with its age and a Google Maps link (admin channels; needs SparkNode1 firmware with the member directory) |

Examples:

- Broad read-only channel: `/spark receive both` (send stays `none`).
- Private admin channel: `/spark admin on`, `/spark send public confirm:True`, `/spark prefix off`.

**Sending to Public from Discord.** Enabling `send public` or `send both` shows this warning and
changes nothing until it is confirmed:

> - Sending to Public from Discord is designed for single-user convenience, not for mass sending or bots.
> - Anyone who can write in this Discord channel will be able to send to Public using your station's credentials and identity.
> - Don't enable it on public or shared Discord channels.

The admin enabling it is the trust decision: there is no per-user allow list. Who enabled it,
and when, is logged (a warning line in the bridge log and the `audit` table) and shown by
`/spark status`. The rate limits apply as before.

**Sender names.** With `prefix on`, a message from Discord is attributed to its author's alias
(`/spark alias`), or else to their Discord display name with `:` and line breaks removed and cut
to 31 bytes. On Public a SparkNode1.2 node sends it as the sender name, so the mesh shows
`BadPirate: Hallo` rather than `<node name>: [BadPirate] Hallo`; an older node gets the
`[name] text` tag. Room posts are authored by the room itself, so they keep the `[name] text` tag
(with the alias).

**Hops, SNR and route (SparkBridge1.3).** Each Public message keeps how it arrived: SNR and hop
count (all SparkNodes), RSSI and the hop hashes of the route (SparkNode1.3, HostLink frame 0xC1).
They are logged, stored with the message's dedup row (`seen_posts`: snr, rssi, hops, path) and
shown in the footer. Hop hashes are translated to names from the adverts the node has heard
(`get names`, GET_CONTACTS, NEW_ADVERT pushes; kept in the `names` table): a hash names a
repeater or room server when exactly one known relay's key starts with it (2-byte hashes make
this reliable; 1-byte hashes often stay hex). Unknown or ambiguous hops are shown in hex; a route
the node had to shorten ends in `→ …`.

**Logging.** Every Discord to mesh send is logged at INFO: channel, target, sender name and length;
Public text is logged shortened, room text never. Received Public messages are logged with SNR,
hops, RSSI and route.

Behaviour details:

- Mesh to Discord uses a channel webhook named by `BRIDGE_WEBHOOK_NAME` (default "Spark Bridge") (found or created through the API,
  cached in memory, token never stored) so the mesh author is the message author. Public: the
  name before the first `: `. Room: name from adverts/contacts, the node name for the room's own
  posts, else `node <8 hex>`. Mentions are always disabled. No webhook permission: fallback to
  bot messages. Bot and webhook messages are never forwarded. No backfill on the first run.
- Discord to mesh is cut on a UTF-8 character boundary: room posts 151 bytes, Public 160 bytes
  minus `<sender name>: ` (or `<node name>: `). A cut message gets a scissors reaction, a link-down message a plug, a
  rate-limited one an hourglass, a node error a cross. Nothing is queued. Attachments and embeds
  are ignored. Newlines become spaces.
- `BRIDGE_PUBLIC_PER_MINUTE` / `BRIDGE_PUBLIC_BURST` is a separate global cap on Public transmissions
  (radio duty cycle) over all channels, on top of the per-channel limit (0 disables it).
- Messages sent from Discord are also shown (author = Discord name) in the other channels whose
  `receive` covers the same source, because the node does not echo them.
- CLI: only in admin channels. The bridge refuses `password`, `set password`,
  `set guest.password`, `set prv.key`, `get prv.key`, `erase`, `start ota` and `set ble.pin`
  unless `BRIDGE_ALLOW_DANGEROUS_CLI=1`; the node additionally refuses serial-only commands.
- Health: battery is sampled every `BRIDGE_HEALTH_SAMPLE_MINUTES`. The daily line (and `/spark health`) shows
  battery at 07:00 and 15:00 (nearest sample within 90 minutes, else `n/a`), the overnight change,
  uptime, noise floor and link availability over 24 h.

## MQTT observer (MC-Radar / DutchMeshCore, Let's Mesh)

With `BRIDGE_OBSERVER=1` the bridge makes the node an MQTT observer ("option 2" on
[meshwiki.nl/wiki/Observer](https://meshwiki.nl/wiki/Observer): the node has no WiFi, the host
publishes). It needs SparkNode1.3: the bridge asks the node for its raw RX feed (HostLink
LOG_RX_DATA) at every connect, and publishes each packet as JSON in the format of
[meshcoretomqtt](https://github.com/Cisien/meshcoretomqtt) and
[meshcore-packet-capture](https://github.com/agessaman/meshcore-packet-capture):

- `meshcore/<IATA>/<NODE PUBKEY>/packets`: `origin`, `origin_id`, `timestamp`, `type` PACKET,
  `direction` rx, `time`, `date`, `len`, `packet_type`, `route` (F/D/T), `payload_len`, `raw`
  (upper-case hex of the packet as received), `SNR`, `RSSI`, `hash` (as `Packet::calculatePacketHash`),
  `path` for direct packets. Not retained, QoS 0.
- `meshcore/<IATA>/<NODE PUBKEY>/status`: `{"status": "online", ...}` with model, firmware,
  radio and the last health sample; retained, refreshed every 5 minutes, `offline` as the
  last will and on a clean stop.

**Login.** The DutchMeshCore collectors (`collector1.dutchmeshcore.nl`, `collector2.dutchmeshcore.nl`,
port 443, MQTT over TLS websockets) and Let's Mesh use token login: user name `v1_<NODE PUBKEY>`,
password a JWT signed with Ed25519 by the node's identity (`aud` = the broker host, valid 60
minutes, renewed by reconnecting). The bridge asks the node to sign it over HostLink (SIGN_*); the
node only signs JWT signing inputs and its private key never leaves it. There is no account, no
registration and no password: the node key is the credential. Optional: `BRIDGE_OBSERVER_OWNER`
(your companion's public key) and `BRIDGE_OBSERVER_EMAIL` link the observer to you. The node clock
must be right (the bridge sets it at every connect). Brokers with a user name and password (URL
form) take the password from the environment variable named by `BRIDGE_OBSERVER_PASSWORD_ENV`.

| Key | Meaning |
|---|---|
| `BRIDGE_OBSERVER` | 1 = on |
| `BRIDGE_OBSERVER_BROKERS` | `dutchmeshcore` (both collectors), `dutchmeshcore-1`, `dutchmeshcore-2`, `letsmesh-eu`, `letsmesh-us`, or URLs `mqtts://host:8883`, `wss://host/path`, comma separated |
| `BRIDGE_OBSERVER_IATA` | region code in the topic (required), e.g. `AMS` |
| `BRIDGE_OBSERVER_NAME` | `origin` name (default: the node name) |
| `BRIDGE_OBSERVER_OWNER`, `_EMAIL` | optional token claims |
| `BRIDGE_OBSERVER_AUTH`, `_USERNAME`, `_PASSWORD_ENV` | URL brokers: token, password (user name + variable name) or none |
| `BRIDGE_OBSERVER_WS_PATH` | websocket path (default `/`, as meshcoretomqtt; some docs give `/mqtt`) |
| `BRIDGE_OBSERVER_TLS_VERIFY`, `_TOKEN_MINUTES`, `_QUEUE` | TLS check, token lifetime, packets kept per broker while it is down |

Legacy `config.toml`: an `[observer]` section with the same names in lower case without the
prefix (`enabled = true`, `brokers = ["dutchmeshcore"]`, `iata = "AMS"`, ...).

Each broker has its own bounded queue (oldest packets dropped) and reconnects with backoff
(2 s doubling to 5 minutes). The node drops feed frames rather than delay its radio when the link
is busy (`get spark` shows `rxlog:<sent>/<dropped>`). `/spark status` shows the observer: per
broker connected or retrying, packets sent and dropped, and the last error.

## Updating the node over Bluetooth (`spark-bridge update`)

For a node that already runs SparkNode1.1 or later (SparkNode1 has no `dfu` command: update it
once over USB with `setup.sh`). Build the package, then run the updater on the bridge host:

```sh
pio run -e SparkNode_P1 -t dfu_zip            # at the repository root -> .pio/build/SparkNode_P1/firmware-dfu.zip
host/.venv/bin/spark-bridge update .pio/build/SparkNode_P1/firmware-dfu.zip
```

What it does, in order (it stops at the first problem):

1. Checks the package offline: application only, device type 0x52, SoftDevice S140 7.3.0,
   CRC16, length, at most 708,608 bytes (the room post store starts above), a sane vector table,
   and the `SPARK-BUILD <version> <id>` tag.
2. Takes the link lock (`spark-bridge.lock` next to the state database). A running bridge
   (systemd, launchd or a terminal) gets SIGUSR1, closes its link, hands the lock over and waits;
   it reconnects by itself when the update is done. If the lock cannot be had, stop the service
   (`sudo systemctl stop spark-bridge`, or `launchctl unload` the agent) and run it again.
3. Over the normal link: reads `get build` and the battery. Refuses the same build and a battery
   below 3700 mV (`--force` overrides both and sends `dfu force`). Asks for confirmation (`--yes`).
4. Sends `dfu`. The node answers with its bootloader address (its own + 1) and resets into the
   bootloader without a bond, so the bootloader is a separate device for macOS and BlueZ.
5. Finds the bootloader by its live advertisement (DFU service), never by a cached name.
6. Transfers with its own DFU code (`dfu.py`): every receipt is checked, a missing receipt is
   checked against the bootloader's byte count, and Reset is never sent after the old app was
   erased, except to the Spark bootloader, which restarts in OTA mode for a retry (3 attempts).
7. Verifies: reconnects to the app and compares `get build` with the package.

On Linux it then removes the temporary bootloader device from BlueZ (`bluetoothctl remove
<MAC+1>`; never the bonded node). On macOS it toggles Bluetooth once with `blueutil` (if
installed) when the updated app cannot be reached. Exit codes: 0 done and verified, 1 failed
with the old app intact, 2 bad config or package, 4 the node has no app and waits in its
bootloader: then `spark-bridge update --in-bootloader <zip>` retries (Spark bootloader), or see
"Recovery" in [spark/README.md](../spark/README.md#update-over-bluetooth).

## Testing without hardware

```sh
pytest                                   # no Bluetooth, hardware, token or internet needed
python -m spark_bridge.stubnode --port 5000    # fake node; stdin: chan Bob: hi | post deadbeef hi | advert Alice | drop
# .env: BRIDGE_TRANSPORT=tcp, BRIDGE_TCP_PORT=5000
spark-bridge --no-discord                       # or with a real token to try the bot against the stub
```

## Contract notes (HostLink v1, not changed)

- **Replay includes our own posts.** "Host-originated room posts are not echoed" cannot hold after
  an APP_START, which replays up to 32 stored posts including ones the host made (authored by the
  room, with a room-clock timestamp the host cannot predict). Workaround, marked in
  `bridge.py` (`on_room_post`): remember a hash of every text posted for 24 h and drop
  room-authored replays that match. Side effect: a `room.post` typed on the node with exactly the
  same text as a recent Discord post is dropped too. A proper fix is a flag or a distinct author
  prefix in the replayed frame, or skipping host-made posts during replay.
- The `meshcore` library sends its own APP_START inside `connect()` and the bridge sends one more to
  read SELF_INFO: two APP_STARTs per connect, harmless.
- The library decodes a room post's 4-byte author prefix as the `signature` field; this works
  because the contract uses txt_type 2 exactly like the companion protocol.
