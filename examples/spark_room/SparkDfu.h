#pragma once
// SpArkNode: pure helpers for the `dfu` command (BLE OTA update through the bootloader).
// No Arduino dependencies, so test_spark_hostlink can test them natively.
//
// `dfu` [force]: reply "OK - dfu <bootloader MAC> ...", then the node drops the bond data the
// bootloader would use, and resets into BLE OTA DFU (GPREGRET 0xA8). Without bond data the
// Adafruit bootloader advertises openly as "AdaDFU" at the app address + 1 (last byte only,
// no carry), so hosts see it as a separate, unbonded device with its own GATT cache.

#include <stddef.h>
#include <stdint.h>

namespace spark {

// Refuse `dfu` below this battery voltage unless `dfu force` (a brownout mid-transfer leaves
// the node without an application).
#ifndef SPARK_DFU_MIN_MV
#define SPARK_DFU_MIN_MV 3700
#endif

// Bootloader peer data ("buttonless DFU" bond hand-over), see Bluefruit's BLEDfu.cpp and the
// bootloader's dfu_ble_svc.c: 60 bytes of address/IRK/LTK/sys attrs, then a CRC16 (SDK
// crc16_compute) over those 60 bytes, in no-init RAM at 0x20007F80.
static const uint32_t DFU_PEER_DATA_ADDR = 0x20007F80UL;
static const size_t DFU_PEER_DATA_LEN = 60;
static const size_t DFU_PEER_DATA_SIZE = 62;
static const uint8_t DFU_MAGIC_OTA_RESET = 0xA8;   // GPREGRET: bootloader starts in BLE OTA mode

enum DfuParse { DFU_CMD_NONE = 0, DFU_CMD_OK, DFU_CMD_FORCE, DFU_CMD_BAD };

// "dfu" -> DFU_CMD_OK, "dfu force" -> DFU_CMD_FORCE, "dfu <other>" -> DFU_CMD_BAD,
// anything else (including "dfux") -> DFU_CMD_NONE.
DfuParse parseDfuCommand(const char* cmd);

// Battery check: true when the update may start.
bool dfuBatteryOk(uint16_t batt_mv, bool force, uint16_t min_mv = SPARK_DFU_MIN_MV);

// The address the bootloader advertises with when it has no bond data: byte 0 (the least
// significant, sd_ble_gap_addr_get order) + 1, wrapping without carry, like the bootloader.
void bootloaderAddress(const uint8_t app_addr[6], uint8_t out[6]);

// "AA:BB:CC:DD:EE:FF", most significant byte first (addr[5] first). out needs 18 bytes.
void formatMac(const uint8_t addr[6], char* out);

// Nordic SDK crc16_compute (CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF).
uint16_t crc16(const uint8_t* data, size_t len, uint16_t crc = 0xFFFF);

// Overwrite peer data so the bootloader rejects it: zeroes, and a CRC that does not match.
void invalidatePeerData(uint8_t peer[DFU_PEER_DATA_SIZE]);

// True if the bootloader would accept this peer data (CRC matches).
bool peerDataValid(const uint8_t peer[DFU_PEER_DATA_SIZE]);

}  // namespace spark
