# HostLink contract v1

HostLink lets a host computer bridge a SparkNode (the spark-core room server) (BLE or USB) to something
else. The contract below is shared byte-for-byte with the host bridge in `host/`; do not
change it without changing both sides.

HostLink is a subset of the MeshCore companion frame protocol (as in `examples/companion_radio/MyMesh.cpp` at tag room-server-v1.18.0), served by the room server. No new frame codes. All integers little-endian. Text is UTF-8, not NUL-terminated inside frames.

**Transports**
- USB serial (115200 8N1) and TCP (stub node only): host->node `0x3C ('<')`, `len_lo`, `len_hi`, payload. node->host `0x3E ('>')`, `len_lo`, `len_hi`, payload.
- BLE: Nordic UART Service `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`; host writes to RX `6E400002-...`, node notifies on TX `6E400003-...`; one frame per write/notification, no header. BLE device name `MeshCore-<node name>` (e.g. `MeshCore-SparkNode`). Link is encrypted with MITM passkey pairing; pairing/bonding is done once at OS level, the PIN is never sent in a frame and never stored by the bridge.
- Max payload 176 bytes in both directions. A node reply goes to the transport the command arrived on. Pushes go to every active transport (BLE: connected and secured; USB: a valid frame was received in the last 120 s).

**Commands (host->node) and replies**

| Code | Name | Payload after code byte | Reply |
|---|---|---|---|
| 1 | APP_START | 7 reserved bytes, then app name | SELF_INFO (5). Side effect: room-post replay cursor := 0, so all stored posts are offered again. If anything is waiting, a MSG_WAITING push follows. |
| 22 | DEVICE_QUERY | u8 app protocol version (host sends 3 or higher) | DEVICE_INFO (13) |
| 5 | GET_DEVICE_TIME | none | CURR_TIME (9): u32 epoch secs |
| 6 | SET_DEVICE_TIME | u32 epoch secs | OK (0), or ERR (1) code 6 if the value is earlier than the node clock |
| 20 | GET_BATT_AND_STORAGE | none | BATT_AND_STORAGE (12): u16 battery mV, u32 used kB, u32 total kB (post store) |
| 56 | GET_STATS | u8 type: 0 core, 1 radio, 2 packets | STATS (24): u8 type, then: core = u16 batt mV, u32 uptime secs, u16 err flags, u8 tx queue len. radio = i16 noise floor dBm, i8 last RSSI, i8 last SNR x4, u32 tx airtime secs, u32 rx airtime secs. packets = u32 recv, u32 sent, u32 sent flood, u32 sent direct, u32 recv flood, u32 recv direct, u32 recv errors. Bad type: ERR code 6. |
| 31 | GET_CHANNEL | u8 idx | CHANNEL_INFO (18): u8 idx, 32-byte NUL-padded name, 16-byte secret. Only idx 0 exists: name `Public`, secret `8b3387e9c5cdea6ac9e5edbaa115cd72`. Other idx: ERR code 2. |
| 32 | SET_CHANNEL | any | ERR code 1 (not supported in v1) |
| 3 | SEND_CHANNEL_TXT_MSG | u8 txt_type (must be 0), u8 channel idx (must be 0), u32 timestamp, text, optional: `00` + sender name (SparkNode1.2, see the amendment below) | OK (0). The node transmits `<sender>: <text>` on the channel, cutting the whole string to 160 bytes; `<sender>` is the sanitized sender name if one was given, otherwise the node name. ERR code 1 for txt_type != 0, ERR code 2 for a bad idx, ERR code 3 if no packet could be allocated. |
| 2 | SEND_TXT_MSG | u8 txt_type, u8 attempt, u32 timestamp, 6-byte destination pubkey prefix, text | If the prefix equals the node's own pubkey prefix and txt_type is 0: the text (cut to 151 bytes) is stored as a room post authored by the room itself and pushed to room clients; reply SENT (6): u8 0, u32 0, u32 0. This post is NOT echoed back to the host. Any other destination: ERR code 2. Other txt_type: ERR code 1. |
| 66 | RUN_CLI_COMMAND | command text | CLI_REPLY (29): reply text (may be empty). Runs the room server CLI with remote (non-serial) privileges: serial-only commands are refused, and `set ble.pin` is always refused here. |
| 10 | SYNC_NEXT_MESSAGE | none | Exactly one of: a room post frame (16), a channel message frame (17), or NO_MORE_MESSAGES (10). Stored room posts newer than the replay cursor come first, oldest first (the cursor advances as each is handed out); then queued channel messages, oldest first. |
| 4 | GET_CONTACTS | optional u32 since | CONTACTS_START (2): u32 count; then count CONTACT (3) frames; then END_OF_CONTACTS (4): u32 most recent lastmod. The list is the node's small RAM cache of recently heard adverts (may be empty). |
| 36 | SEND_TRACE_PATH | u32 tag, u32 auth, u8 flags (lower 2 bits: hop hash size 1/2/4/8 bytes), path bytes (hop hashes; >= 1 byte, multiple of the hop size, <= 64 hops) | SENT (6): u8 0, u32 tag, u32 est timeout ms (30000). ERR code 6 for a bad path, code 3 if no packet could be allocated. Sends a direct-routed TRACE exactly like the upstream companion. |
| any other | | | ERR (1) code 1 |

Push `0x89` TRACE_DATA (upstream layout): u8 0, u8 path_len (bytes of hashes), u8 flags, u32 tag, u32 auth, hashes[path_len], i8 snr x4 per hop (SNR at which that hop heard its predecessor), i8 final snr x4 (this node hearing the returning packet). Pushed for every trace that ends at this node; the Python `meshcore` `send_trace()` / `EventType.TRACE_DATA` work unchanged.

**Reply and push layouts**
- OK: `00`. ERR: `01`, u8 code (1 unsupported, 2 not found, 3 table full, 4 bad state, 6 illegal arg).
- SELF_INFO: `05`, u8 adv_type = 3 (room), u8 tx power dBm, u8 max tx power, 32-byte pubkey, i32 lat x1e6, i32 lon x1e6, u8 multi_acks, u8 advert loc policy, u8 0, u8 0, u32 freq (MHz x1000), u32 bandwidth (kHz x1000), u8 SF, u8 CR, node name (rest of frame).
- DEVICE_INFO: `0d`, u8 firmware protocol code = 14, u8 0, u8 max channels = 1, u32 0 (the BLE PIN is never disclosed), 12-byte NUL-padded build date, 40-byte NUL-padded board model, 20-byte NUL-padded version string (e.g. `v1.18.0-SparkNode1.1`, exactly 20 bytes, so no NUL terminator is guaranteed: read it as a fixed field), u8 repeat enabled (0/1), u8 path hash mode. 82 bytes.
- Room post (CONTACT_MSG_RECV_V3): `10`, i8 0, `00 00`, 6-byte prefix of the ROOM's own pubkey, u8 `ff`, u8 txt_type = 2, u32 post timestamp (room clock), 4-byte prefix of the AUTHOR's pubkey, text. A post whose author prefix equals the room's own prefix was posted by the room itself (USB/remote CLI `room.post`).
- Channel message (CHANNEL_MSG_RECV_V3): `11`, i8 SNR x4, `00 00`, u8 channel idx, u8 path_len (`ff` = direct, otherwise top 2 bits hash mode and low 6 bits hop count), u8 txt_type = 0, u32 sender timestamp, text in the form `<sender name>: <message>`.
- NO_MORE_MESSAGES: `0a`.
- CONTACT (3) and push NEW_ADVERT (0x8A) share one layout after the code byte: 32-byte pubkey, u8 advert type (1 chat, 2 repeater, 3 room, 4 sensor), u8 flags = 0, u8 out_path_len = `ff`, 64 zero bytes, 32-byte NUL-padded name, u32 last advert timestamp, i32 lat x1e6, i32 lon x1e6, u32 lastmod. 148 bytes.
- Push MSG_WAITING: `83`. Sent when a room post is stored or a channel message is queued while a host is active, and after APP_START if anything is waiting. The host then calls SYNC_NEXT_MESSAGE until NO_MORE_MESSAGES.
- Push NEW_ADVERT: `8a` + contact layout, sent when an advert with a name is heard while a host is active.

**Semantics both sides rely on**
- Delivery of room posts is at-least-once (replay after every APP_START, up to 32 posts). The host deduplicates on (post timestamp, author prefix, text).
- Channel messages received while no host is active are kept in a RAM ring of 32 (oldest dropped, lost on reboot).
- Host-originated room posts are not echoed to the host. Host-originated channel messages are not received back from the mesh. The host still keeps a short "recently sent" filter.
- The node never blocks on the host. With no host, relaying, room login, post storage and post sync to mesh clients continue unchanged.
- Limits: channel text on air is 160 bytes including the `<sender>: ` prefix (node name or host-supplied sender); a room post is 151 bytes. The host truncates on a UTF-8 character boundary before sending.

**Golden vectors** (hex; pubkey of the example node starts `a1b2c3d4e5f6`, timestamp 1700000000)
- V1 host->node channel send "[robin] radio check": `03000000f153655b726f62696e5d20726164696f20636865636b`
- V2 node->host channel message "Bob: hi", SNR 5.0, 2 hops: `1114000000020000f15365426f623a206869`
- V3 node->host room post "hello room" by author `deadbeef`: `10000000a1b2c3d4e5f6ff0200f15365deadbeef68656c6c6f20726f6f6d`
- V4 host->node room post "[robin] hi room": `02000000f15365a1b2c3d4e5f65b726f62696e5d20686920726f6f6d`, reply `06000000000000000000`
- V5 stats core (4012 mV, uptime 86400 s, no errors, queue 0): `1800ac0f80510100000000`
- V6 stats radio (noise -112, RSSI -95, SNR 7.5, tx 120 s, rx 3600 s): `180190ffa11e78000000100e0000`
- V7 battery 4012 mV, storage 0/0: `0cac0f0000000000000000`
- V8 CLI "get repeat": `4267657420726570656174`, reply "> on": `1d3e206f6e`
- V9 the V8 command on the serial/TCP wire: `3c0b004267657420726570656174`; its reply on the wire: `3e05001d3e206f6e`
- V10 host->node channel send "Hallo" as sender "BadPirate" (SparkNode1.2): `03000000f1536548616c6c6f00426164506972617465`; on air: `BadPirate: Hallo`

## Amendment: host-originated room posts (firmware rule, no frame change)

A room post created through HostLink (SEND_TXT_MSG to the node's own key prefix) is
recorded as host-originated, and the mark is persisted with the post (flag trailer of the
`/posts` file). Host-originated posts are excluded from the live MSG_WAITING push and from
every SYNC_NEXT_MESSAGE replay, including the replay after APP_START and after a node
reboot. Posts from mesh clients and from the USB or remote `room.post` CLI are delivered as
before. Limit: the mark is kept for the 32 most recent host posts, which is at least as many
as the room holds.

## Amendment: sender name for channel sends (SparkNode1.2)

SEND_CHANNEL_TXT_MSG may carry the name to show as the sender on the channel, so a bridge can
post `BadPirate: Hallo` instead of `<node name>: BadPirate: Hallo`:

```
03 | u8 txt_type=0 | u8 chan_idx=0 | u32 timestamp | text | [ 00 | sender name ]
```

- The trailer starts at the first NUL byte in the text field; the sender name runs to the frame
  end or to a second NUL (anything after that is ignored). Text and name are UTF-8.
- The node sanitizes the name: `:` is removed, control characters (newline, CR, tab) become
  spaces, runs of spaces collapse, the ends are trimmed, and it is cut to 31 bytes (MeshCore's
  32-byte name field) on a UTF-8 boundary. If nothing is left, or the trailer is missing or
  empty, the node name is used as before.
- The 160-byte on-air limit now counts `<sender>: `; the host budgets with the sanitized name.
- Backward compatible both ways: a frame without a NUL is the v1 frame. Firmware before
  SparkNode1.2 stops the text at the NUL, so it sends `<node name>: <text>` and ignores the
  name; a host should only send the trailer when DEVICE_INFO reports SparkNode1.2 or later
  (the bridge falls back to tagging the text itself).
- Room posts (SEND_TXT_MSG) are unchanged: the room is the author; a host tags the text.

## CLI commands added by spark-core

| Command | Privilege | Effect |
|---|---|---|
| `set ble.pin <6 digits>` | USB text CLI only (refused from LoRa and HostLink) | store the BLE PIN; `OK - reboot to apply`. No default PIN exists; without one BLE is never started |
| `get ble.pin` | any | `> set` or `> unset`, never the value |
| `set ble on` / `set ble off` | any admin | enable/disable BLE; takes effect at once when BLE is running (kill switch). BLE starts 30 s after boot |
| `get ble` | any | enabled, running, connected, pin set |
| `ble unpair` | any admin | clear bonds, disconnect |
| `get spark` | any | version, store on/off, posts, queued channel messages, active hosts |
| `trace <hex>[,<hex>...]` | any | send a direct TRACE through the listed repeaters (hash prefixes of 1, 2 or 4 bytes, all the same size, up to 6 hops); `OK - trace sent tag=XXXXXXXX`. The result is also printed on USB serial |
| `trace` | any | last trace: tag, then per hop `<hash> X.XdB` (SNR at which that hop heard the previous sender), then `back X.XdB rssi=N` (the return as this node heard it); or `pending`, `timeout (no reply after 30 s)`, `none` |
| `get names [start]` | any | advert-name cache (32 entries, newest first): 8 hex pubkey prefix and name; `more: get names N` continues. Use the first 2, 4 or 8 hex digits as a trace hop |
| `set repeat on\|off`, `get repeat` | any admin | relaying toggle (`ROOM_ALLOW_REPEAT_TOGGLE`) |
| `start ota` | refused while BLE runs | use the BLE DFU service |
| `members [start]` | any admin (`ROOM_MEMBER_DIR`) | member directory: `<8 hex> <name>[*] <last seen>` per line (`*` = admin), `more: members N` continues |
| `member del <hex prefix\|name>` | any admin | forget a member (their kids' devices drop them from People at the next sync) |
| `member name <hex prefix> <name>` | any admin | rename a member (a later advert or GET_MEMBERS from that member renames it again) |
| `where [name\|start]` | any admin | latest GPS fix per member: `<name> <age> <lat>,<lon> s<sats>` (6 decimals), `<name> never` for a named member without a fix, `no locations`, or `more: where N`. Parse lines from the right: names may contain spaces |

## Behaviour not fixed by the contract

- Malformed known commands (too short) answer ERR code 6; an empty room post answers ERR 6.
- SET_DEVICE_TIME accepts a value equal to the node clock.
- A USB frame is `<` + 2 length bytes + payload; frames are only recognised when the CLI
  line buffer is empty. Do not mix text CLI use and frames in the same second.

## Amendment: member directory and locations (SparkNode1, `ROOM_MEMBER_DIR`)

No HostLink frame changes. The room server keeps a directory of every client that logged in
with the room or admin password (full pubkey, name, admin flag, last seen, latest GPS fix),
persisted in `/members` on InternalFS (format `SMD1`, see `examples/spark_room/MemberDir.h`).
Clients use two new mesh request types (PAYLOAD_TYPE_REQ, after the usual 4-byte tag):

| Req | Name | Body | Reply after the reflected tag |
|---|---|---|---|
| `0xA0` | GET_MEMBERS | u8 start, optional requester name (<= 15 bytes, NUL terminated); the name becomes the requester's directory name | `a0`, u8 total, u8 start, u8 count, then per member: 32-byte pubkey, u8 flags (bit 0 admin), u8 name_len, name. Reply <= 120 bytes incl. tag; ask again with start += count |
| `0xA1` | SET_LOCATION | i32 lat x1e6, i32 lon x1e6, u32 fix time (GPS epoch secs), u8 satellites | `a1`, u8 status (0 stored, 1 rejected) |

Both require room (read/write) or admin permission; guests get no reply. Locations are never sent
to clients: only the admin CLI (`where`, over USB, LoRa admin login or HostLink RUN_CLI_COMMAND)
shows them. Location writes are flushed to flash at most every 15 minutes.

Names: when a member names itself through GET_MEMBERS (or an admin uses `member name`), the
room puts that name into the HostLink name cache as if the member's advert had been heard: it
shows in `get names` and GET_CONTACTS and is pushed as NEW_ADVERT to an active host. The cache
is also seeded with every directory name after boot. The bridge additionally reads `members` at
link-up and when an unknown author posts (at most every 5 minutes).
