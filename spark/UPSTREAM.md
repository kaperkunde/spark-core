# Upstream handling

- `main` mirrors `upstream/main` (fast-forward only). Spark's work sits on MeshCore tag
  `room-server-v1.18.0` (the same commit as `companion-v1.18.0` / `repeater-v1.18.0`).
  New upstream room-server tags are merged in (`git merge <tag>`, not rebase, once the branch
  is public).
- Upstream takes PRs against `dev`, one feature per PR: `git switch -c pr/<topic> upstream/dev`,
  cherry-pick the single change, open the PR from the fork.

## Versions and tags

Each part has its own counter: **SparkNode1**, **SparkBridge1**, **SparkKids1**, and so on. The
MeshCore base stays visible: the node reports `v1.18.0-SparkNode1`, SparkKids shows
`SparkKids1 (MeshCore v1.18.0)`, the bridge package is `spark-bridge` version `1`
(`spark-bridge --version` prints `SparkBridge1`). Git tags use the same names (`SparkNode1`,
`SparkBridge1`, `SparkKids1`). Version strings live in `variants/spark_p1/platformio.ini`
(`FIRMWARE_VERSION`), `variants/spark_tdeck/platformio.ini` (`KIDS_FW_VERSION`) and
`host/pyproject.toml` + `host/src/spark_bridge/__init__.py`.

## Hooks in upstream files (all inside `#ifdef`)

| File | Hook | Flag |
|---|---|---|
| `examples/simple_room_server/MyMesh.h` | includes, `friend class PostPersistAccess/HostLink/MemberDirAccess`, overrides (`searchChannelsByHash`, `onGroupDataRecv`, `onAdvertRecv`, `onTraceRecv`) | `ROOM_PERSIST_POSTS`, `WITH_HOSTLINK`, `ROOM_MEMBER_DIR` |
| `examples/simple_room_server/MyMesh.cpp` | `storePost` notify, `begin` restore, `loop` tick, `handleCommand` spark commands, repeat branch, login + `handleRequest` member hooks, new-install path hash mode default | the five above, `SPARK_PATH_HASH_MODE` |
| `examples/simple_room_server/main.cpp` | `hostlink_setup()`, serial demux for `<` frames | `WITH_HOSTLINK` |
| `.gitignore`, `README.md` | `.env` and local tool dirs ignored; Spark introduction on top of the MeshCore README | - |

Everything else is new: `examples/spark_room/`, `examples/spark_kids/`, `variants/spark_p1/`,
`variants/spark_tdeck/`, `test/test_spark_*`, `spark/`, `host/`, `setup.sh`, `.env.example`,
`.github/workflows/spark.yml` (CI: `native_spark`, bridge pytest, both firmware builds).
No upstream ini file is edited; the root `platformio.ini` globs `variants/*/platformio.ini`.
`spark/pio_env.py` replaces inherited `-D` definitions (e.g. `LORA_SF`) with the `.env` values
instead of stacking redefinitions.

## PR candidates (one commit each, most to least likely to be accepted)

1. Clock floor after reboot for room servers (part of the persistence code; split it when
   proposing: posts are otherwise invisible to clients after a power cycle).
2. Room post persistence behind `ROOM_PERSIST_POSTS`.
3. `CompanionProtocol.h`: share the frame constants instead of keeping them private to `companion_radio/MyMesh.cpp`.
4. Public channel receive/send on the room server.
5. HostLink itself (larger; offer after it has run for a while).
6. Repeat toggle: upstream removed it on purpose; propose only as a build flag.
