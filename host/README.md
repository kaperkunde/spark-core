# Spark Bridge (SparkBridge1)

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
| `/spark send none\|public\|room\|both` | Discord to mesh target |
| `/spark prefix on\|off` | on: `[discord-name] text`; off: plain text, sent as the node itself |
| `/spark ratelimit none\|channel\|user [per_minute]` | no limit, or N messages/minute per channel or per user (default 3) |
| `/spark receive none\|public\|room\|both` | mesh to Discord source copied into this channel |
| `/spark report on\|off` | the daily health line (`BRIDGE_HEALTH_REPORT`, default 15:05 in `BRIDGE_TZ`) and link up/down notices here |
| `/spark health` | health now, live from the node (admin channels) |
| `/spark cli command:<text>` | node CLI command (admin channels) |
| `/spark where [name]` | latest GPS fix of every room member (or one), with its age and a Google Maps link (admin channels; needs SparkNode1 firmware with the member directory) |

Examples:

- Broad read-only channel: `/spark receive both` (send stays `none`).
- Private admin channel: `/spark admin on`, `/spark send public`, `/spark prefix off`.

Behaviour details:

- Mesh to Discord uses a channel webhook named by `BRIDGE_WEBHOOK_NAME` (default "Spark Bridge") (found or created through the API,
  cached in memory, token never stored) so the mesh author is the message author. Public: the
  name before the first `: `. Room: name from adverts/contacts, the node name for the room's own
  posts, else `node <8 hex>`. Mentions are always disabled. No webhook permission: fallback to
  bot messages. Bot and webhook messages are never forwarded. No backfill on the first run.
- Discord to mesh is cut on a UTF-8 character boundary: room posts 151 bytes, Public 160 bytes
  minus `<node name>: `. A cut message gets a scissors reaction, a link-down message a plug, a
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
