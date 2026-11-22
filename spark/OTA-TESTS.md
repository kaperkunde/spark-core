# Bluetooth update: hardware tests

Run these with the node **on the desk**, connected over USB, batteries in, before relying on
Bluetooth updates on the roof. `spark/tools/ota_hwtest.sh --port <node USB port>` walks through
them in this order, asks before every step that writes to the node and logs the results to
`out/ota-test/`. `--from <ID>` resumes at a step. Recovery for every step: double-press reset,
copy a UF2 to XIAO-BOOT (keep the running release's UF2 at hand; `--rollback` records it).

| # | Test | Pass |
|---|---|---|
| P0 / T0 | Build SparkNode2 (UF2 + DFU zip), a second build with another build id (`SPARK_BUILD_SALT`), the bootloader; `pio test -e native_spark`, bridge `pytest` | builds and all tests green |
| H1 | Double-press, read `INFO_UF2.TXT` | `Board-ID: nRF52840-SeeedXiao-v1`; note the bootloader version (stock 0.9.2 or a Seeed build?) |
| H2 | Copy `update-xiao_nrf52840_ble_bootloader-0.11.0-spark1_nosd.uf2` to XIAO-BOOT; double-press again | INFO_UF2.TXT shows `0.11.0-spark1`, the app starts, identity and settings unchanged; `get spark` post count (posts may be lost: `posts reset` if damaged) |
| H3 | Copy SparkNode2 `firmware.uf2` over USB | `ver` / `get build` show the expected build; the bridge reconnects without re-pairing |
| T1 | Bridge stopped; `spark-bridge scan` | one `app` row (NUS) for the node; note the LEDs in app mode |
| T2 | `dfu` on the USB console; `spark-bridge scan`; press reset | a `bootloader` row (AdaDFU) at the address from the reply (node + 1), connectable without a bond; after reset the same build, posts, identity |
| T4 | `spark-bridge update` 5 times: the same build with `--force`, then alternating builds; bridge service running | 5 of 5 verified, no Bluetooth toggle, the bridge pauses and reconnects by itself, posts and ACL intact |
| T5 | `dfu`, wait 60 s, then `spark-bridge update --in-bootloader`; again `dfu` and press reset | the bootloader still waits after 60 s and the tool completes; reset returns to the old app |
| T6a | Ctrl-C during an update at about 30 %, then `--in-bootloader` | the tool reports "no application"; `scan` shows the bootloader; the retry resets the dirty session and completes |
| T6b | Same break, then power-cycle the node | after power-on the bootloader advertises (Bluetooth, not USB); `--in-bootloader` completes |
| T6c | Same break, then double-press reset | XIAO-BOOT appears (USB recovery still works); UF2 copy restores the app |
| T7 | After the updates: the bonded Mac and Pi reconnect without manual steps; optionally an image with a changed GATT table | both re-discover by themselves (Service Changed) |
| T8 | From the Pi at its final location: 5 updates, RSSI from `spark-bridge scan` | 5 of 5 |
| T9 | `dfu`, then the nRF Connect or nRF DFU phone app with the zip | the app comes back with the zip's build id: the no-ladder fallback works |
| T3 (optional) | The old bonded route (the app's BLEDfu service from a bonded Mac), then `get build` over USB | settles whether yesterday's "DFU Complete" had worked; not needed otherwise |

What each failure means:

- H1 shows another Board-ID: stop; the Spark bootloader build is not for this board.
- H2: the drive does not come back after the copy and nothing advertises: the bootloader copy
  failed; SWD is the only recovery (spark/README.md, "Risks").
- T6b comes up as XIAO-BOOT instead of advertising: the Spark bootloader is not installed or
  the patch is not active; do not use Bluetooth updates on the roof.
