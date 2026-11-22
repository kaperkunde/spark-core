#include "SparkDfu.h"

#include <stdio.h>
#include <string.h>

namespace spark {

DfuParse parseDfuCommand(const char* cmd) {
  if (strncmp(cmd, "dfu", 3) != 0) return DFU_CMD_NONE;
  const char* p = cmd + 3;
  if (*p == 0) return DFU_CMD_OK;
  if (*p != ' ') return DFU_CMD_NONE;
  while (*p == ' ') p++;
  if (*p == 0) return DFU_CMD_OK;
  if (strcmp(p, "force") == 0) return DFU_CMD_FORCE;
  return DFU_CMD_BAD;
}

bool dfuBatteryOk(uint16_t batt_mv, bool force, uint16_t min_mv) { return force || batt_mv >= min_mv; }

void bootloaderAddress(const uint8_t app_addr[6], uint8_t out[6]) {
  memcpy(out, app_addr, 6);
  out[0] = (uint8_t)(out[0] + 1);
}

void formatMac(const uint8_t a[6], char* out) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X", a[5], a[4], a[3], a[2], a[1], a[0]);
}

uint16_t crc16(const uint8_t* data, size_t len, uint16_t crc) {
  for (size_t i = 0; i < len; i++) {
    crc = (uint16_t)((uint8_t)(crc >> 8) | (crc << 8));
    crc ^= data[i];
    crc ^= (uint8_t)(crc & 0xFF) >> 4;
    crc ^= (uint16_t)((crc << 8) << 4);
    crc ^= (uint16_t)(((crc & 0xFF) << 4) << 1);
  }
  return crc;
}

void invalidatePeerData(uint8_t peer[DFU_PEER_DATA_SIZE]) {
  memset(peer, 0, DFU_PEER_DATA_SIZE);
  uint16_t bad = (uint16_t)(crc16(peer, DFU_PEER_DATA_LEN) + 1);
  peer[DFU_PEER_DATA_LEN] = (uint8_t)bad;
  peer[DFU_PEER_DATA_LEN + 1] = (uint8_t)(bad >> 8);
}

bool peerDataValid(const uint8_t peer[DFU_PEER_DATA_SIZE]) {
  uint16_t stored = (uint16_t)(peer[DFU_PEER_DATA_LEN] | (peer[DFU_PEER_DATA_LEN + 1] << 8));
  return stored == crc16(peer, DFU_PEER_DATA_LEN);
}

}  // namespace spark
