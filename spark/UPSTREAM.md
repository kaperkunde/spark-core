# Upstream handling

- `main` mirrors `upstream/main` (fast-forward only). SpArk's work sits on MeshCore tag
  `room-server-v1.18.0` (the same commit as `companion-v1.18.0` / `repeater-v1.18.0`).
  New upstream room-server tags are merged in (`git merge <tag>`, not rebase, once the branch
  is public).
- Upstream takes PRs against `dev`, one feature per PR: `git switch -c pr/<topic> upstream/dev`,
  cherry-pick the single change, open the PR from the fork.

## Versions and tags

Each part has its own counter: **SpArkNode1.3**, **SpArkBridge1.3**, **SpArkKids1**, and so on. The
MeshCore base stays visible: the node reports `v1.18.0-SpArkNode1.3`, SpArkKids shows
`SpArkKids1 (MeshCore v1.18.0)`, the bridge package is `spark-bridge` version `1.3`
(`spark-bridge --version` prints `SpArkBridge1.3`). SpArkNode1.1 and SpArkBridge1.1 add Bluetooth
updates (`dfu`, `spark-bridge update`); the node also reports a build id (`get build`).
SpArkNode1.2 and SpArkBridge1.2 add the HostLink sender name (Discord aliases on Public), the
confirmation for Discord -> Public send, and the repeater's region/loop checks in the room
server's relay. SpArkNode1.3 and SpArkBridge1.3 add the MQTT observer (raw RX feed, node-signed
tokens), channel routes with hops/SNR footers, and send logging. Git tags use the version names.
Tags made before the SpArk spelling keep the old casing (`SparkNode1`, `SparkBridge1`,
`SparkKids1`, `SparkNode1.1`, `SparkBridge1.1`, `SparkNode1.2`, `SparkBridge1.2`); from 1.3 on
tags use the new one (`SpArkNode1.3`, `SpArkBridge1.3`). Hosts read both spellings of the
version label, case-insensitively. `FIRMWARE_VERSION` is limited to 20 bytes
(`v1.18.0-SpArkNode1.3` is exactly 20): a bug-fix release keeps that label and sets
`-D SPARK_RELEASE_PATCH=<n>` (see HOSTLINK.md, "Version labels"). Version strings live in `variants/spark_p1/platformio.ini`
(`FIRMWARE_VERSION`), `variants/spark_tdeck/platformio.ini` (`KIDS_FW_VERSION`) and
`host/pyproject.toml` + `host/src/spark_bridge/__init__.py`.

## Hooks in upstream files (all inside `#ifdef`)

| File | Hook | Flag |
|---|---|---|
| `examples/simple_room_server/MyMesh.h` | includes, `friend class PostPersistAccess/HostLink/MemberDirAccess`, overrides (`searchChannelsByHash`, `onGroupDataRecv`, `onAdvertRecv`, `onTraceRecv`) | `ROOM_PERSIST_POSTS`, `WITH_HOSTLINK`, `ROOM_MEMBER_DIR` |
| `examples/simple_room_server/MyMesh.cpp` | `allowPacketForward`: region (`region denyf`) and `loop.detect` checks as in `simple_repeater` (not behind a flag: an upstream bug fix, PR candidate) | - |
| `src/helpers/RoutingPolicy.h` | `mesh::isFloodLooped()`: the repeater's loop check as a pure function, tested in `test/test_routing_policy` | - |
| `examples/simple_room_server/MyMesh.cpp` | `storePost` notify, `begin` restore, `loop` tick, `handleCommand` spark commands, repeat branch, login + `handleRequest` member hooks, new-install path hash mode default | the five above, `SPARK_PATH_HASH_MODE` |
| `examples/simple_room_server/main.cpp` | `hostlink_setup()`, serial demux for `<` frames | `WITH_HOSTLINK` |
| `src/helpers/nrf52/SerialBLEInterface.cpp` | `onSecured`: one Service Changed indication per boot | `SPARK_SERVICE_CHANGED` |
| `.gitignore`, `README.md` | `.env` and local tool dirs ignored; SpArk introduction on top of the MeshCore README | - |

Everything else is new: `examples/spark_room/`, `examples/spark_kids/`, `variants/spark_p1/`,
`variants/spark_tdeck/`, `test/test_spark_*`, `spark/`, `host/`, `setup.sh`, `.env.example`,
`.github/workflows/spark.yml` (CI: `native_spark`, bridge pytest, both firmware builds).
No upstream ini file is edited; the root `platformio.ini` globs `variants/*/platformio.ini`.
`spark/pio_env.py` replaces inherited `-D` definitions (e.g. `LORA_SF`) with the `.env` values
instead of stacking redefinitions.

## PR candidates (one commit each, most to least likely to be accepted)

1. Room server relay: drop denied-region floods and loops as the repeater does (`allowPacketForward`,
   `mesh::isFloodLooped`); a plain bug fix.
2. Clock floor after reboot for room servers (part of the persistence code; split it when
   proposing: posts are otherwise invisible to clients after a power cycle).
3. Room post persistence behind `ROOM_PERSIST_POSTS`.
4. `CompanionProtocol.h`: share the frame constants instead of keeping them private to `companion_radio/MyMesh.cpp`.
5. Public channel receive/send on the room server.
6. HostLink itself (larger; offer after it has run for a while).
7. Repeat toggle: upstream removed it on purpose; propose only as a build flag.
8. Service Changed on the first secured connection (`SerialBLEInterface.cpp`): useful for every
   nRF52 BLE firmware whose GATT table can change with an update.

## Bootloader

`spark/bootloader/` builds [Adafruit_nRF52_Bootloader](https://github.com/adafruit/Adafruit_nRF52_Bootloader)
tag `0.11.0` (pinned commit `c67f0bc`) for `xiao_nrf52840_ble` with S140 7.3.0,
`DEFAULT_TO_OTA_DFU=1` and `OTA_DFU_ONLY_WITHOUT_APP=1` from
`0001-ota-dfu-only-without-app.patch`. Version string `0.11.0-spark1` (a local tag, so the
Makefile's `git describe` yields it). The patch is a candidate for an upstream PR: upstream's
`DEFAULT_TO_OTA_DFU` also turns double reset and the app's UF2/serial DFU requests into OTA DFU,
so a board with that flag cannot reach its UF2 drive any more. To move to a newer release:
change `TAG`/`COMMIT`/`SPARK_TAG` in `build.sh`, re-apply the patch, and repeat the bench tests
H1, H2, T5 and T6 in OTA-TESTS.md. Notes: 0.11.0's `board.cmake`/Makefile default for nRF52840
is S140 6.1.1, so `SD_VERSION=7.3.0` must stay explicit; its Makefile needs GNU sed for the
version number, `build.sh` passes it.
