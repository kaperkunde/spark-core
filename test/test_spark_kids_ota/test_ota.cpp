#include <gtest/gtest.h>
#include <string.h>
#include <string>
// Units under test are included as sources (see test_spark_hostlink for why). lib/ed25519 is
// compiled by the native_spark environment (build_src_filter).
#include "../../examples/spark_kids/KidsLogic.cpp"
#include <ed_25519.h>

using namespace kids::ota;

static void hex(const char* h, uint8_t* out, int n) {
  for (int i = 0; i < n; i++) { char b[3] = { h[2 * i], h[2 * i + 1], 0 }; out[i] = (uint8_t)strtoul(b, NULL, 16); }
}

// Same vector as host/tests/test_kidsota.py::test_cross_vector_matches_native_test: seed 00..1f,
// code 102, size 123456, sha256("spark kids ota test image"), signed by the bridge's pure-Python
// Ed25519 and verified here by MeshCore's orlp/ed25519 (what the T-Deck runs).
static const char* PUB = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8";
static const char* SHA = "f4d9b324c6c70b260be502c4fb8293a61545de165f45ec9401fab659419f0d81";
static const char* SIG = "7aaa779b981bbd9478ca705e6916e23d1fa3ba627c5eb6a5251de1d5c4273942"
                         "9ea9f3dcac7954d32d25c52990d25c8384e2e2d8dc86332eb563a3bc3e6ed703";

static size_t makeOffer(uint8_t* b, uint32_t code, uint32_t size, const char* label) {
  b[0] = CMD_OFFER;
  memcpy(b + 1, &code, 4);
  memcpy(b + 5, &size, 4);
  hex(SHA, b + 9, 32);
  hex(SIG, b + 41, 64);
  memcpy(b + OFFER_LEN, label, strlen(label));
  return OFFER_LEN + strlen(label);
}

TEST(KidsOta, PythonSignatureVerifiesWithOrlp) {
  uint8_t buf[200], pub[32];
  hex(PUB, pub, 32);
  Offer o;
  ASSERT_TRUE(parseOffer(buf, makeOffer(buf, 102, 123456, "SpArkKids-test102"), o));
  EXPECT_EQ(o.code, 102u);
  EXPECT_EQ(o.size, 123456u);
  EXPECT_STREQ(o.label, "SpArkKids-test102");
  uint8_t msg[SIGNED_LEN];
  signedMessage(msg, o.code, o.size, o.sha);
  EXPECT_EQ(memcmp(msg, "SPARK-KIDS-OTA-1", 16), 0);
  EXPECT_EQ(ed25519_verify(o.sig, msg, sizeof(msg), pub), 1);
  // a different code (downgrade/upgrade replay with a forged code) or size breaks the signature
  signedMessage(msg, 103, o.size, o.sha);
  EXPECT_EQ(ed25519_verify(o.sig, msg, sizeof(msg), pub), 0);
  signedMessage(msg, o.code, o.size + 1, o.sha);
  EXPECT_EQ(ed25519_verify(o.sig, msg, sizeof(msg), pub), 0);
  uint8_t other[32];
  memcpy(other, pub, 32);
  other[0] ^= 1;
  signedMessage(msg, o.code, o.size, o.sha);
  EXPECT_EQ(ed25519_verify(o.sig, msg, sizeof(msg), other), 0);
}

TEST(KidsOta, ParseOfferRejectsBadInput) {
  uint8_t buf[200];
  Offer o;
  size_t n = makeOffer(buf, 102, 5000, "");
  EXPECT_TRUE(parseOffer(buf, n, o));
  EXPECT_FALSE(parseOffer(buf, n - 1, o));                 // short
  EXPECT_FALSE(parseOffer(buf, OFFER_LEN + LABEL_MAX + 1, o));   // label too long
  buf[0] = CMD_ABORT;
  EXPECT_FALSE(parseOffer(buf, n, o));
  n = makeOffer(buf, 102, 5000, "a\x01" "b");
  ASSERT_TRUE(parseOffer(buf, n, o));
  EXPECT_STREQ(o.label, "a?b");                            // control characters never reach the screen
}

TEST(KidsOta, CheckOffer) {
  Offer o;
  memset(&o, 0, sizeof(o));
  o.code = 102; o.size = 800000;
  EXPECT_EQ(checkOffer(o, 101, 0x640000), R_NONE);
  EXPECT_EQ(checkOffer(o, 102, 0x640000), R_NOT_NEWER);
  EXPECT_EQ(checkOffer(o, 200, 0x640000), R_NOT_NEWER);
  EXPECT_EQ(checkOffer(o, 101, 700000), R_TOO_BIG);
  o.size = 10;
  EXPECT_EQ(checkOffer(o, 101, 0x640000), R_BAD_OFFER);
}

TEST(KidsOta, BatteryRule) {
  EXPECT_TRUE(batteryOk(3700, false));
  EXPECT_FALSE(batteryOk(3699, false));
  EXPECT_TRUE(batteryOk(3300, true));    // USB power: any battery
  EXPECT_FALSE(batteryOk(0, false));     // no reading: not without USB
}

TEST(KidsOta, Snooze) {
  EXPECT_EQ(snoozeLeft(0, 1000), 0u);
  EXPECT_EQ(snoozeLeft(1000 + SNOOZE_SECS, 1000), SNOOZE_SECS);
  EXPECT_EQ(snoozeLeft(5000, 4000), 1000u);
  EXPECT_EQ(snoozeLeft(4000, 5000), 0u);
  EXPECT_EQ(snoozeLeft(0xFFFFFFF0u, 100), SNOOZE_SECS);   // never more than 12 h
}

TEST(KidsOta, WireFormatsMatchTheBridge) {
  uint8_t b[64];
  const uint8_t fam[4] = { 1, 2, 3, 4 };
  ASSERT_EQ(encodeAdvert(b, fam, 101, ADV_SNOOZED), (size_t)ADVERT_LEN);
  const uint8_t adv[12] = { 'S', 'K', 1, 1, 2, 3, 4, 101, 0, 0, 0, 1 };   // kidsfw.encode_advert
  EXPECT_EQ(memcmp(b, adv, 12), 0);
  ASSERT_EQ(encodeState(b, ST_SNOOZED, R_NONE, 43200), 7u);
  const uint8_t st[7] = { 0x81, 2, 0, 0xC0, 0xA8, 0, 0 };
  EXPECT_EQ(memcmp(b, st, 7), 0);
  ASSERT_EQ(encodeOffset(b, NTF_ACK, 4096), 5u);
  const uint8_t ack[5] = { 0x82, 0, 0x10, 0, 0 };
  EXPECT_EQ(memcmp(b, ack, 5), 0);
  size_t n = encodeStatus(b, sizeof(b), 101, ADV_LOW_BATTERY, 60, 3650, "SpArkKids1.1");
  ASSERT_EQ(n, (size_t)(STATUS_LEN + 12));
  const uint8_t stat[12] = { 1, 101, 0, 0, 0, 2, 60, 0, 0, 0, 0x42, 0x0E };
  EXPECT_EQ(memcmp(b, stat, 12), 0);
  EXPECT_EQ(memcmp(b + 12, "SpArkKids1.1", 12), 0);
}

TEST(KidsOta, ShortLabel) {
  char out[32];
  shortLabel("SpArkKids1.1 (MeshCore v1.18.0)", out, sizeof(out));
  EXPECT_STREQ(out, "SpArkKids1.1");
  shortLabel("abcdefghijklmnopqrstuvwxyz0123", out, sizeof(out));
  EXPECT_EQ(strlen(out), (size_t)LABEL_MAX);
}

TEST(KidsOta, ReceiveSequencing) {
  EXPECT_EQ(rxCheck(0, 1000, 0, 200), RX_OK);
  EXPECT_EQ(rxCheck(200, 1000, 0, 200), RX_DUP);       // resent after a NACK: ignore
  EXPECT_EQ(rxCheck(200, 1000, 100, 200), RX_GAP);     // partial overlap: ask from 200
  EXPECT_EQ(rxCheck(200, 1000, 400, 200), RX_GAP);     // lost packet: NACK 200
  EXPECT_EQ(rxCheck(900, 1000, 900, 200), RX_OVERRUN); // past the announced size
  EXPECT_EQ(rxCheck(900, 1000, 900, 100), RX_OK);
  EXPECT_EQ(rxCheck(0, 1000, 0, 0), RX_OVERRUN);
}

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
