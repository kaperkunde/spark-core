#include <gtest/gtest.h>
#include <string.h>
// Included as a source, like the other Spark tests (see test_spark_hostlink).
#include "../../examples/spark_room/SparkDfu.cpp"

using namespace spark;

TEST(SparkDfu, ParseCommand) {
  EXPECT_EQ(DFU_CMD_OK, parseDfuCommand("dfu"));
  EXPECT_EQ(DFU_CMD_OK, parseDfuCommand("dfu "));
  EXPECT_EQ(DFU_CMD_FORCE, parseDfuCommand("dfu force"));
  EXPECT_EQ(DFU_CMD_FORCE, parseDfuCommand("dfu   force"));
  EXPECT_EQ(DFU_CMD_BAD, parseDfuCommand("dfu now"));
  EXPECT_EQ(DFU_CMD_BAD, parseDfuCommand("dfu forced"));
  EXPECT_EQ(DFU_CMD_NONE, parseDfuCommand("dfux"));
  EXPECT_EQ(DFU_CMD_NONE, parseDfuCommand("df"));
  EXPECT_EQ(DFU_CMD_NONE, parseDfuCommand("start ota"));
  EXPECT_EQ(DFU_CMD_NONE, parseDfuCommand(""));
}

TEST(SparkDfu, BatteryGuard) {
  EXPECT_TRUE(dfuBatteryOk(4100, false));
  EXPECT_TRUE(dfuBatteryOk(SPARK_DFU_MIN_MV, false));
  EXPECT_FALSE(dfuBatteryOk(SPARK_DFU_MIN_MV - 1, false));
  EXPECT_FALSE(dfuBatteryOk(0, false));   // no reading (USB only, no cells): needs force
  EXPECT_TRUE(dfuBatteryOk(0, true));
  EXPECT_TRUE(dfuBatteryOk(3300, true));
  EXPECT_FALSE(dfuBatteryOk(3799, false, 3800));
}

TEST(SparkDfu, BootloaderAddressIsLastBytePlusOneWithoutCarry) {
  // sd_ble_gap_addr_get order: addr[0] is the least significant byte
  const uint8_t app[6] = {0x10, 0x32, 0x54, 0x76, 0x98, 0xDA};
  uint8_t bl[6];
  bootloaderAddress(app, bl);
  char s[18];
  formatMac(bl, s);
  EXPECT_STREQ("DA:98:76:54:32:11", s);
  formatMac(app, s);
  EXPECT_STREQ("DA:98:76:54:32:10", s);

  const uint8_t wrap[6] = {0xFF, 0x00, 0x00, 0x00, 0x00, 0xC0};
  bootloaderAddress(wrap, bl);
  formatMac(bl, s);
  EXPECT_STREQ("C0:00:00:00:00:00", s);   // the bootloader does addr[0] += 1 on a uint8_t
}

TEST(SparkDfu, Crc16MatchesNordicSdk) {
  // Nordic crc16_compute == CRC-16/CCITT-FALSE; check value of "123456789" is 0x29B1
  EXPECT_EQ(0x29B1, crc16((const uint8_t*)"123456789", 9));
  EXPECT_EQ(0xFFFF, crc16(nullptr, 0));
  // chaining
  uint16_t c = crc16((const uint8_t*)"1234", 4);
  EXPECT_EQ(0x29B1, crc16((const uint8_t*)"56789", 5, c));
}

TEST(SparkDfu, InvalidatePeerData) {
  uint8_t peer[DFU_PEER_DATA_SIZE];
  // a valid hand-over, as Bluefruit's BLEDfu writes it
  for (size_t i = 0; i < DFU_PEER_DATA_LEN; i++) peer[i] = (uint8_t)(i * 7 + 3);
  uint16_t c = crc16(peer, DFU_PEER_DATA_LEN);
  peer[60] = (uint8_t)c;
  peer[61] = (uint8_t)(c >> 8);
  EXPECT_TRUE(peerDataValid(peer));

  invalidatePeerData(peer);
  EXPECT_FALSE(peerDataValid(peer));
  for (size_t i = 0; i < DFU_PEER_DATA_LEN; i++) EXPECT_EQ(0, peer[i]);

  // all zeroes with a zero CRC must not be valid either (CRC of 60 zero bytes is not 0)
  memset(peer, 0, sizeof(peer));
  EXPECT_FALSE(peerDataValid(peer));
}

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
