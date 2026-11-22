#include <gtest/gtest.h>
#include <string>
#include <vector>
#include <string.h>
// The units under test are included as sources so that both `pio test -e native` (which
// builds every test dir with a fixed source filter) and `-e native_spark` can run this.
#include "../../examples/spark_room/HostLinkCodec.cpp"
#include "../../examples/spark_room/PostStore.cpp"
#include "../../examples/spark_room/HostPosts.h"
#include "../../examples/spark_room/CompanionProtocol.h"

using namespace hl;

static std::string hex(const uint8_t* p, size_t n) {
  static const char* d = "0123456789abcdef";
  std::string s;
  for (size_t i = 0; i < n; i++) { s += d[p[i] >> 4]; s += d[p[i] & 15]; }
  return s;
}
static std::vector<uint8_t> unhex(const std::string& h) {
  std::vector<uint8_t> v;
  for (size_t i = 0; i + 1 < h.size(); i += 2) v.push_back((uint8_t)strtol(h.substr(i, 2).c_str(), 0, 16));
  return v;
}

static const uint32_t TS = 1700000000;
static const uint8_t ROOM_KEY[6] = {0xa1, 0xb2, 0xc3, 0xd4, 0xe5, 0xf6};

// ---- golden vectors V1..V9 ----

TEST(Golden, V1_ChannelSendParse) {
  auto f = unhex("03000000f153655b726f62696e5d20726164696f20636865636b");
  ChannelSendCmd c;
  ASSERT_TRUE(parseChannelSend(f.data(), f.size(), c));
  EXPECT_EQ(0, c.txt_type);
  EXPECT_EQ(0, c.chan_idx);
  EXPECT_EQ(TS, c.timestamp);
  EXPECT_EQ("[robin] radio check", std::string(c.text, c.text_len));
}

TEST(Golden, V2_ChannelMsgBuild) {
  uint8_t out[MAX_FRAME];
  size_t n = buildChannelMsg(out, 20, 0, 2, TS, "Bob: hi", 7);
  EXPECT_EQ("1114000000020000f15365426f623a206869", hex(out, n));
}

TEST(Golden, V3_RoomPostBuild) {
  uint8_t out[MAX_FRAME];
  const uint8_t author[4] = {0xde, 0xad, 0xbe, 0xef};
  size_t n = buildRoomPost(out, ROOM_KEY, TS, author, "hello room", 10);
  EXPECT_EQ("10000000a1b2c3d4e5f6ff0200f15365deadbeef68656c6c6f20726f6f6d", hex(out, n));
}

TEST(Golden, V4_RoomPostSendParseAndSentReply) {
  auto f = unhex("02000000f15365a1b2c3d4e5f65b726f62696e5d20686920726f6f6d");
  TxtSendCmd c;
  ASSERT_TRUE(parseTxtSend(f.data(), f.size(), c));
  EXPECT_EQ(0, c.txt_type);
  EXPECT_EQ(TS, c.timestamp);
  EXPECT_EQ(0, memcmp(c.dest6, ROOM_KEY, 6));
  EXPECT_EQ("[robin] hi room", std::string(c.text, c.text_len));
  uint8_t out[MAX_FRAME];
  EXPECT_EQ("06000000000000000000", hex(out, buildSent(out)));
}

TEST(Golden, V5_StatsCore) {
  uint8_t out[MAX_FRAME];
  EXPECT_EQ("1800ac0f80510100000000", hex(out, buildStatsCore(out, 4012, 86400, 0, 0)));
}

TEST(Golden, V6_StatsRadio) {
  uint8_t out[MAX_FRAME];
  EXPECT_EQ("180190ffa11e78000000100e0000", hex(out, buildStatsRadio(out, -112, -95, 30, 120, 3600)));
}

TEST(Golden, V7_BattStorage) {
  uint8_t out[MAX_FRAME];
  EXPECT_EQ("0cac0f0000000000000000", hex(out, buildBattStorage(out, 4012, 0, 0)));
}

TEST(Golden, V8_CliReply) {
  auto cmd = unhex("4267657420726570656174");
  EXPECT_EQ(CMD_RUN_CLI_COMMAND, cmd[0]);
  EXPECT_EQ("get repeat", std::string((const char*)&cmd[1], cmd.size() - 1));
  uint8_t out[MAX_FRAME];
  EXPECT_EQ("1d3e206f6e", hex(out, buildCliReply(out, "> on")));
}

TEST(Golden, V9_Wire) {
  auto cmd = unhex("4267657420726570656174");
  std::vector<uint8_t> wire = unhex("3c0b004267657420726570656174");
  UsbFrameParser p;
  UsbFrameParser::Result r = UsbFrameParser::NONE;
  for (uint8_t b : wire) r = p.feed(b, 0);
  ASSERT_EQ(UsbFrameParser::FRAME, r);
  EXPECT_EQ(cmd.size(), p.length());
  EXPECT_EQ(0, memcmp(p.payload(), cmd.data(), cmd.size()));
  uint8_t reply[MAX_FRAME];
  size_t rl = buildCliReply(reply, "> on");
  uint8_t out[MAX_FRAME + 3];
  EXPECT_EQ("3e05001d3e206f6e", hex(out, wrapWire(out, reply, rl)));
}

// ---- sizes ----

TEST(Layout, DeviceInfoIs82Bytes) {
  uint8_t out[MAX_FRAME];
  DeviceInfo d = {"20 Nov 2026", "Seeed SenseCAP Solar P1", "v1.18.0-spark1", 1, 0};
  size_t n = buildDeviceInfo(out, d);
  EXPECT_EQ(82u, n);
  EXPECT_EQ(RESP_CODE_DEVICE_INFO, out[0]);
  EXPECT_EQ(14, out[1]);
  EXPECT_EQ(1, out[3]);
  EXPECT_EQ(0, memcmp(&out[4], "\0\0\0\0", 4));
  EXPECT_EQ(0, memcmp(&out[8], "20 Nov 2026\0", 12));
  EXPECT_NE(nullptr, strstr((const char*)&out[8 + 12 + 40], "spark"));
  EXPECT_EQ(1, out[80]);
  EXPECT_EQ(0, out[81]);
}

TEST(Layout, DeviceInfoKeepsA20ByteVersion) {
  uint8_t out[MAX_FRAME];
  DeviceInfo d = {"20 Nov 2026", "Seeed SenseCAP Solar P1", "v1.18.0-SparkNode1.2", 1, 0};
  ASSERT_EQ(82u, buildDeviceInfo(out, d));
  EXPECT_EQ(0, memcmp(&out[8 + 12 + 40], "v1.18.0-SparkNode1.2", 20));
  EXPECT_EQ(1, out[80]);
}

TEST(Layout, ContactIs148Bytes) {
  uint8_t out[MAX_FRAME];
  Contact c;
  memset(&c, 0, sizeof(c));
  memset(c.pubkey, 0xAB, 32);
  c.adv_type = 1;
  strcpy(c.name, "Bob");
  c.last_advert_ts = TS; c.lat = -33000000; c.lon = 151000000; c.lastmod = 7;
  size_t n = buildContact(out, RESP_CODE_CONTACT, c);
  EXPECT_EQ(148u, n);
  EXPECT_EQ(3, out[0]);
  EXPECT_EQ(0xFF, out[35]);   // out_path_len
  EXPECT_EQ(0, out[36]);
  EXPECT_EQ(0, memcmp(&out[100], "Bob\0", 4));
  n = buildContact(out, PUSH_CODE_NEW_ADVERT, c);
  EXPECT_EQ(148u, n);
  EXPECT_EQ(0x8A, out[0]);
}

TEST(Layout, SelfInfo) {
  uint8_t out[MAX_FRAME];
  SelfInfo s;
  memset(&s, 0, sizeof(s));
  s.adv_type = 3; s.tx_power = 22; s.max_tx_power = 22;
  s.freq_khz = 915000; s.bw_hz = 250000; s.sf = 10; s.cr = 5; s.name = "Spark";
  size_t n = buildSelfInfo(out, s);
  EXPECT_EQ(58u + 5u, n);
  EXPECT_EQ(5, out[0]);
  EXPECT_EQ(3, out[1]);
  EXPECT_EQ(0, memcmp(&out[n - 5], "Spark", 5));
  EXPECT_EQ(10, out[n - 5 - 2]);
}

TEST(Layout, ChannelInfoAndMisc) {
  uint8_t out[MAX_FRAME];
  uint8_t secret[16] = {0x8b, 0x33, 0x87, 0xe9, 0xc5, 0xcd, 0xea, 0x6a, 0xc9, 0xe5, 0xed, 0xba, 0xa1, 0x15, 0xcd, 0x72};
  size_t n = buildChannelInfo(out, 0, "Public", secret);
  EXPECT_EQ(50u, n);
  EXPECT_EQ(0, memcmp(&out[2], "Public\0", 7));
  EXPECT_EQ(0, memcmp(&out[34], secret, 16));
  EXPECT_EQ("0106", hex(out, buildErr(out, 6)));
  EXPECT_EQ("00", hex(out, buildOk(out)));
  EXPECT_EQ("0a", hex(out, buildNoMore(out)));
  EXPECT_EQ("83", hex(out, buildMsgWaiting(out)));
  EXPECT_EQ("0900f15365", hex(out, buildCurrTime(out, TS)));
  EXPECT_EQ("0207000000", hex(out, buildContactsStart(out, 7)));
  EXPECT_EQ("04c0000000", hex(out, buildEndOfContacts(out, 192)));
  EXPECT_EQ(30u, buildStatsPackets(out, 1, 2, 3, 4, 5, 6, 7));
}

// ---- truncation ----

TEST(Truncate, ChannelTextCutsAt160) {
  std::string long_text(300, 'x');
  char out[MAX_CHAN_TEXT + 1];
  size_t n = composeChannelText(out, "Spark", long_text.data(), long_text.size());
  EXPECT_EQ(160u, n);
  EXPECT_EQ(0, strncmp(out, "Spark: xxx", 10));
  EXPECT_EQ(0, out[160]);
}

TEST(Truncate, Utf8NotSplit) {
  // 2-byte char U+00E9 repeated: cut at an odd byte must back up
  std::string t;
  for (int i = 0; i < 100; i++) t += "\xC3\xA9";
  EXPECT_EQ(150u, truncUtf8(t.data(), t.size(), 151));
  EXPECT_EQ(150u, truncUtf8(t.data(), t.size(), 150));
  EXPECT_EQ(5u, truncUtf8("hello", 5, 151));
  // 4-byte emoji straddling the limit
  std::string e = std::string(149, 'a') + "\xF0\x9F\x98\x80";
  EXPECT_EQ(149u, truncUtf8(e.data(), e.size(), 151));
  EXPECT_EQ(153u, truncUtf8(e.data(), e.size(), 153));
}

TEST(Truncate, ChannelTextUtf8Boundary) {
  std::string t = std::string(155, 'a') + "\xF0\x9F\x98\x80" + "tail";
  char out[MAX_CHAN_TEXT + 1];
  size_t n = composeChannelText(out, "N", t.data(), t.size());   // "N: " = 3 bytes
  EXPECT_EQ(3u + 155u, n);   // emoji would end at 3+159 > 160, dropped whole
  EXPECT_LE(n, 160u);
}

TEST(Truncate, RoomPostFrameCutAtFrameLimit) {
  uint8_t out[MAX_FRAME];
  std::string t(300, 'z');
  const uint8_t author[4] = {1, 2, 3, 4};
  size_t n = buildRoomPost(out, ROOM_KEY, TS, author, t.data(), t.size());
  EXPECT_EQ(MAX_FRAME, n);
}

TEST(Parse, RejectsShortCommands) {
  uint8_t f[8] = {3, 0, 0, 0, 0, 0, 0, 0};
  ChannelSendCmd c;
  EXPECT_FALSE(parseChannelSend(f, 6, c));
  EXPECT_TRUE(parseChannelSend(f, 7, c));   // empty text allowed at parse level
  TxtSendCmd t;
  f[0] = 2;
  EXPECT_FALSE(parseTxtSend(f, 8, t));
}

// ---- channel send with a sender name (SparkNode1.2 trailer) ----

static std::string sanitize(const std::string& in) {
  char out[MAX_SENDER_NAME + 1];
  size_t n = sanitizeSenderName(out, in.data(), in.size());
  EXPECT_EQ(n, strlen(out));
  return std::string(out, n);
}

TEST(Golden, V10_ChannelSendWithSender) {
  auto f = unhex("03000000f1536548616c6c6f00426164506972617465");
  ChannelSendCmd c;
  ASSERT_TRUE(parseChannelSend(f.data(), f.size(), c));
  EXPECT_EQ(TS, c.timestamp);
  EXPECT_EQ("Hallo", std::string(c.text, c.text_len));
  ASSERT_NE(nullptr, c.sender);
  EXPECT_EQ("BadPirate", std::string(c.sender, c.sender_len));
  char name[MAX_SENDER_NAME + 1];
  ASSERT_EQ(9u, sanitizeSenderName(name, c.sender, c.sender_len));
  char out[MAX_CHAN_TEXT + 1];
  size_t n = composeChannelText(out, name, c.text, c.text_len);
  EXPECT_EQ("BadPirate: Hallo", std::string(out, n));
}

TEST(SenderName, V1WithoutTrailerHasNoSender) {
  auto f = unhex("03000000f153655b726f62696e5d20726164696f20636865636b");
  ChannelSendCmd c;
  ASSERT_TRUE(parseChannelSend(f.data(), f.size(), c));
  EXPECT_EQ(nullptr, c.sender);
  EXPECT_EQ(0u, c.sender_len);
  EXPECT_EQ(19u, c.text_len);
}

TEST(SenderName, EmptyTrailerAndSecondNul) {
  std::vector<uint8_t> f = {3, 0, 0, 0, 0, 0, 0, 'h', 'i', 0};
  ChannelSendCmd c;
  ASSERT_TRUE(parseChannelSend(f.data(), f.size(), c));
  EXPECT_EQ("hi", std::string(c.text, c.text_len));
  EXPECT_EQ(0u, c.sender_len);   // "hi\0": no sender, node name is used
  std::vector<uint8_t> g = {3, 0, 0, 0, 0, 0, 0, 'h', 'i', 0, 'A', 'n', 0, 'x'};
  ASSERT_TRUE(parseChannelSend(g.data(), g.size(), c));
  EXPECT_EQ("hi", std::string(c.text, c.text_len));
  EXPECT_EQ("An", std::string(c.sender, c.sender_len));   // ends at the second NUL
  std::vector<uint8_t> h = {3, 0, 0, 0, 0, 0, 0, 0, 'A', 'n'};
  ASSERT_TRUE(parseChannelSend(h.data(), h.size(), c));
  EXPECT_EQ(0u, c.text_len);
  EXPECT_EQ("An", std::string(c.sender, c.sender_len));
}

TEST(SenderName, SanitizeDropsColonsAndNewlines) {
  EXPECT_EQ("BadPirate", sanitize("BadPirate"));
  EXPECT_EQ("Bad Pirate", sanitize("  Bad:\nPirate:  "));
  EXPECT_EQ("a b c", sanitize("a\r\n\tb \x7f c"));
  EXPECT_EQ("", sanitize(":::"));
  EXPECT_EQ("", sanitize(" \n "));
  EXPECT_EQ("", sanitize(""));
  EXPECT_EQ("ab", sanitize(std::string("ab\0cd", 5)));   // stops at a NUL
}

TEST(SenderName, SanitizeCutsTo31BytesOnUtf8Boundary) {
  EXPECT_EQ(std::string(31, 'x'), sanitize(std::string(40, 'x')));
  // 15 x 2-byte chars = 30 bytes, a 16th would need bytes 31-32
  std::string e;
  for (int i = 0; i < 20; i++) e += "\xC3\xA9";
  EXPECT_EQ(30u, sanitize(e).size());
  // 30 ASCII + a 4-byte emoji: the emoji does not fit
  EXPECT_EQ(std::string(30, 'a'), sanitize(std::string(30, 'a') + "\xF0\x9F\x98\x80"));
  // 27 ASCII + emoji = 31 bytes: fits exactly
  std::string fit = std::string(27, 'a') + "\xF0\x9F\x98\x80";
  EXPECT_EQ(fit, sanitize(fit + "zz"));
  // a space at the cut is trimmed
  EXPECT_EQ(std::string(30, 'a'), sanitize(std::string(30, 'a') + " bcd"));
}

TEST(SenderName, ComposeWithLongestSenderKeeps160) {
  std::string name = sanitize(std::string(40, 'N'));
  std::string t(300, 't');
  char out[MAX_CHAN_TEXT + 1];
  size_t n = composeChannelText(out, name.c_str(), t.data(), t.size());
  EXPECT_EQ(160u, n);
  EXPECT_EQ(0, strncmp(out, (name + ": t").c_str(), name.size() + 3));
}

// ---- USB frame parser ----

static UsbFrameParser::Result feedAll(UsbFrameParser& p, const std::vector<uint8_t>& v, uint32_t t = 0) {
  UsbFrameParser::Result r = UsbFrameParser::NONE;
  for (uint8_t b : v) {
    r = p.feed(b, t);
    if (r != UsbFrameParser::NONE) break;
  }
  return r;
}

TEST(UsbParser, GoodFrame) {
  UsbFrameParser p;
  EXPECT_EQ(UsbFrameParser::FRAME, feedAll(p, {'<', 3, 0, 0x16, 0x03, 0x00}));
  EXPECT_EQ(3u, p.length());
  EXPECT_EQ(0x16, p.payload()[0]);
  EXPECT_FALSE(p.inFrame());
}

TEST(UsbParser, ZeroLengthAborts) {
  UsbFrameParser p;
  EXPECT_EQ(UsbFrameParser::ABORT, feedAll(p, {'<', 0, 0}));
  EXPECT_FALSE(p.inFrame());
  // recovers
  EXPECT_EQ(UsbFrameParser::FRAME, feedAll(p, {'<', 1, 0, 5}));
}

TEST(UsbParser, OversizeAborts) {
  UsbFrameParser p;
  EXPECT_EQ(UsbFrameParser::ABORT, feedAll(p, {'<', 177, 0}));
  EXPECT_EQ(UsbFrameParser::ABORT, feedAll(p, {'<', 0, 1}));
  std::vector<uint8_t> max = {'<', 176, 0};
  max.insert(max.end(), 176, 0x55);
  EXPECT_EQ(UsbFrameParser::FRAME, feedAll(p, max));
  EXPECT_EQ(176u, p.length());
}

TEST(UsbParser, SplitDelivery) {
  UsbFrameParser p;
  EXPECT_EQ(UsbFrameParser::NONE, feedAll(p, {'<'}, 10));
  EXPECT_TRUE(p.inFrame());
  EXPECT_EQ(UsbFrameParser::NONE, feedAll(p, {4}, 20));
  EXPECT_EQ(UsbFrameParser::NONE, feedAll(p, {0, 9, 8}, 30));
  EXPECT_EQ(UsbFrameParser::FRAME, feedAll(p, {7, 6}, 40));
  EXPECT_EQ(4u, p.length());
  EXPECT_EQ(9, p.payload()[0]);
  EXPECT_EQ(6, p.payload()[3]);
}

TEST(UsbParser, IdleIgnoresNonMarker) {
  UsbFrameParser p;
  EXPECT_EQ(UsbFrameParser::NONE, p.feed('g', 0));
  EXPECT_FALSE(p.inFrame());
}

TEST(UsbParser, TimeoutDropsPartialFrame) {
  UsbFrameParser p;
  feedAll(p, {'<', 5, 0, 1}, 1000);
  EXPECT_TRUE(p.inFrame());
  EXPECT_FALSE(p.tick(1400));
  EXPECT_TRUE(p.tick(1500));
  EXPECT_FALSE(p.inFrame());
  EXPECT_FALSE(p.tick(9999));
}

// ---- post file codec ----

using namespace spark;

static PostRec mk(uint32_t ts, uint8_t fill, size_t tl, char c) {
  PostRec r;
  memset(&r, 0, sizeof(r));
  r.timestamp = ts;
  memset(r.author, fill, 32);
  r.text_len = (uint8_t)tl;
  memset(r.text, c, tl);
  r.text[tl] = 0;
  return r;
}

static void roundTrip(int n, size_t tl) {
  std::vector<PostRec> recs;
  for (int i = 0; i < n; i++) { recs.push_back(mk(1700000000 + i, (uint8_t)(i + 1), tl, 'a' + (i % 26))); recs.back().flags = (i % 3 == 0) ? POST_FLAG_HOST : 0; }
  std::vector<uint8_t> buf(POSTFILE_HEADER_SIZE + 32 * (POST_REC_MAX + 1));
  size_t len = postFileEncode(buf.data(), buf.size(), 1700000999, recs.data(), (uint8_t)n);
  ASSERT_EQ(POSTFILE_HEADER_SIZE + (size_t)n * (POST_REC_FIXED + tl + 1), len);
  std::vector<PostRec> back(32);
  uint32_t lk = 0;
  uint8_t cnt = 99;
  ASSERT_TRUE(postFileDecode(buf.data(), len, lk, back.data(), 32, cnt));
  EXPECT_EQ(1700000999u, lk);
  ASSERT_EQ(n, cnt);
  for (int i = 0; i < n; i++) {
    EXPECT_EQ(recs[i].timestamp, back[i].timestamp);
    EXPECT_EQ(0, memcmp(recs[i].author, back[i].author, 32));
    EXPECT_EQ(recs[i].flags, back[i].flags);
    EXPECT_EQ(recs[i].text_len, back[i].text_len);
    EXPECT_STREQ(recs[i].text, back[i].text);
  }
}

TEST(PostFile, RoundTripEmpty) { roundTrip(0, 0); }
TEST(PostFile, RoundTripOne) { roundTrip(1, 10); }
TEST(PostFile, RoundTrip32) { roundTrip(32, 40); }
TEST(PostFile, RoundTrip32Max) { roundTrip(32, 151); }
TEST(PostFile, RoundTripEmptyText) { roundTrip(3, 0); }

TEST(PostFile, HeaderBytes) {
  uint8_t h[POSTFILE_HEADER_SIZE];
  postEncodeHeader(h, 2, 0x01020304);
  EXPECT_EQ("5350503101020000" "04030201", hex(h, sizeof(h)));
}

TEST(PostFile, RejectsCorrupt) {
  std::vector<PostRec> recs = {mk(5, 1, 10, 'a'), mk(6, 2, 10, 'b')};
  std::vector<uint8_t> buf(512);
  size_t len = postFileEncode(buf.data(), buf.size(), 7, recs.data(), 2);
  std::vector<PostRec> back(32);
  uint32_t lk;
  uint8_t cnt;
  EXPECT_FALSE(postFileDecode(buf.data(), len - 3, lk, back.data(), 32, cnt));   // truncated inside a record
  EXPECT_FALSE(postFileDecode(buf.data(), 5, lk, back.data(), 32, cnt));
  std::vector<uint8_t> bad = buf;
  bad[0] = 'X';
  EXPECT_FALSE(postFileDecode(bad.data(), len, lk, back.data(), 32, cnt));
  bad = buf; bad[4] = 2;
  EXPECT_FALSE(postFileDecode(bad.data(), len, lk, back.data(), 32, cnt));
  bad = buf; bad[POSTFILE_HEADER_SIZE + 36] = 200;   // text_len > 151
  EXPECT_FALSE(postFileDecode(bad.data(), len, lk, back.data(), 32, cnt));
  EXPECT_FALSE(postFileDecode(buf.data(), len, lk, back.data(), 1, cnt));   // more than max_recs
}

TEST(PostFile, FilesWithoutFlagTrailerStillLoad) {
  std::vector<PostRec> recs = {mk(5, 1, 10, 'a'), mk(6, 2, 4, 'b')};
  recs[0].flags = POST_FLAG_HOST;
  std::vector<uint8_t> buf(512);
  size_t len = postFileEncode(buf.data(), buf.size(), 7, recs.data(), 2);
  std::vector<PostRec> back(32);
  uint32_t lk;
  uint8_t cnt;
  ASSERT_TRUE(postFileDecode(buf.data(), len - 2, lk, back.data(), 32, cnt));   // trailer cut off
  EXPECT_EQ(2, cnt);
  EXPECT_EQ(0, back[0].flags);
  EXPECT_EQ(0, back[1].flags);
}

TEST(HostPostsSet, MarkAndLookup) {
  EXPECT_FALSE(HostPosts::has(42));
  HostPosts::mark(42);
  EXPECT_TRUE(HostPosts::has(42));
  for (uint32_t i = 100; i < 100 + HostPosts::N; i++) HostPosts::mark(i);   // ring wraps
  EXPECT_FALSE(HostPosts::has(42));
  EXPECT_TRUE(HostPosts::has(100 + HostPosts::N - 1));
}

TEST(BuildDate, EpochFromCDate) {
  EXPECT_EQ(1700000000u - 1700000000u % 86400u, epochFromCDate("Nov 14 2023"));
  EXPECT_EQ(1767225600u, epochFromCDate("Jan  1 2026"));
  EXPECT_EQ(1795132800u, epochFromCDate("Nov 20 2026"));
  EXPECT_EQ(951782400u + 0u, epochFromCDate("Feb 29 2000"));
  EXPECT_EQ(0u, epochFromCDate("garbage"));
  EXPECT_EQ(0u, epochFromCDate(nullptr));
}

TEST(PostFile, EncodeCapacityCheck) {
  std::vector<PostRec> recs = {mk(5, 1, 100, 'a')};
  uint8_t small[50];
  EXPECT_EQ(0u, postFileEncode(small, sizeof(small), 1, recs.data(), 1));
}

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}

// ---- Public channel init (regression: _public used to stay zeroed, so hash 0x00 / zero key) ----
#include "../../examples/spark_room/PublicChannel.h"

namespace {
struct FakeChannel { uint8_t hash[1]; uint8_t secret[32]; };
// Not a real SHA256 (native tests have none): records its inputs so we can check what init feeds it.
static uint8_t g_sha_in[64]; static int g_sha_len = -1;
void fakeSha(uint8_t* hash, size_t hash_len, const uint8_t* msg, int len) {
  g_sha_len = len; memcpy(g_sha_in, msg, len);
  memset(hash, 0x11, hash_len);   // 0x11 = SHA256(Public key)[0], verified with openssl
}
}

TEST(PublicChannel, KeyIsTheUpstreamPublicPsk) {
  // base64 izOH6cXN6mrJ5e26oRXNcg== (docs/faq.md)
  auto k = unhex("8b3387e9c5cdea6ac9e5edbaa115cd72");
  ASSERT_EQ(16u, k.size());
  EXPECT_EQ(0, memcmp(k.data(), spark::PUBLIC_CHANNEL_SECRET, 16));
}

TEST(PublicChannel, InitSetsSecretAndHash) {
  FakeChannel ch; memset(&ch, 0, sizeof(ch));
  spark::initPublicChannel(ch, fakeSha);
  EXPECT_EQ(0, memcmp(ch.secret, spark::PUBLIC_CHANNEL_SECRET, 16));
  for (int i = 16; i < 32; i++) EXPECT_EQ(0, ch.secret[i]);
  EXPECT_EQ(16, g_sha_len);   // hashed over the 16 key bytes, like BaseChatMesh::addChannel
  EXPECT_EQ(0, memcmp(g_sha_in, spark::PUBLIC_CHANNEL_SECRET, 16));
  EXPECT_EQ(0x11, ch.hash[0]);
}

TEST(PublicChannel, HashMatchRejectsZeroChannel) {
  FakeChannel zeroed; memset(&zeroed, 0, sizeof(zeroed));
  uint8_t pub_hash[1] = {0x11}, zero_hash[1] = {0};
  EXPECT_FALSE(spark::channelHashMatches(zeroed, pub_hash));   // the bug: uninitialised never matched
  EXPECT_TRUE(spark::channelHashMatches(zeroed, zero_hash));
  FakeChannel ch; spark::initPublicChannel(ch, fakeSha);
  EXPECT_TRUE(spark::channelHashMatches(ch, pub_hash));
}

// ---- TRACE ----

TEST(Trace, ParseHopsSizes) {
  uint8_t o[MAX_TRACE_BYTES]; size_t n; uint8_t fl;
  ASSERT_TRUE(parseTraceHops("2d", o, n, fl));
  EXPECT_EQ(1u, n); EXPECT_EQ(0, fl); EXPECT_EQ(0x2d, o[0]);
  ASSERT_TRUE(parseTraceHops("2dbb3446", o, n, fl));
  EXPECT_EQ(4u, n); EXPECT_EQ(2, fl); EXPECT_EQ("2dbb3446", hex(o, n));
  ASSERT_TRUE(parseTraceHops("2dbb, 5F01 ,aabb", o, n, fl));
  EXPECT_EQ(6u, n); EXPECT_EQ(1, fl); EXPECT_EQ("2dbb5f01aabb", hex(o, n));
}

TEST(Trace, ParseHopsRejects) {
  uint8_t o[MAX_TRACE_BYTES]; size_t n; uint8_t fl;
  EXPECT_FALSE(parseTraceHops("", o, n, fl));
  EXPECT_FALSE(parseTraceHops("2d,", o, n, fl));
  EXPECT_FALSE(parseTraceHops(",2d", o, n, fl));
  EXPECT_FALSE(parseTraceHops("2d,2dbb", o, n, fl));    // mixed sizes
  EXPECT_FALSE(parseTraceHops("2dbb34", o, n, fl));     // 3 bytes
  EXPECT_FALSE(parseTraceHops("2", o, n, fl));          // odd digits
  EXPECT_FALSE(parseTraceHops("zz", o, n, fl));
  EXPECT_FALSE(parseTraceHops("2d 5f", o, n, fl));      // missing comma
  EXPECT_FALSE(parseTraceHops("01,02,03,04,05,06,07", o, n, fl));   // 7 hops
  EXPECT_TRUE(parseTraceHops("01,02,03,04,05,06", o, n, fl));
}

TEST(Trace, SendCmdParse) {
  // python lib: cmd, tag, auth, flags, path   (tag 0x11223344, auth 0, flags 2, one 4-byte hop)
  auto f = unhex("2444332211000000000" "2" "2dbb3446");
  TraceSendCmd c;
  ASSERT_TRUE(parseTraceSend(f.data(), f.size(), c, 64));
  EXPECT_EQ(0x11223344u, c.tag); EXPECT_EQ(0u, c.auth); EXPECT_EQ(2, c.flags);
  EXPECT_EQ(4, c.path_len); EXPECT_EQ("2dbb3446", hex(c.path, c.path_len));
  auto bad = unhex("24443322110000000002" "2dbb34");           // 3 bytes, 4-byte hops
  EXPECT_FALSE(parseTraceSend(bad.data(), bad.size(), c, 64));
  auto empty = unhex("2444332211000000000000");                  // python pads an empty path to 11
  EXPECT_TRUE(parseTraceSend(empty.data(), empty.size(), c, 64)); // upstream accepts it: 1 byte hop 00
  auto shortf = unhex("24443322110000000000");
  EXPECT_FALSE(parseTraceSend(shortf.data(), shortf.size(), c, 64));
}

TEST(Trace, SentAndDataFrames) {
  uint8_t o[MAX_FRAME];
  EXPECT_EQ(10u, buildTraceSent(o, 0x11223344, 30000));
  EXPECT_EQ("06004433221130750000", hex(o, 10));
  // 1 hop of 4 bytes: python parser reads hashes then one snr per hop then the final snr
  uint8_t h[4] = {0x2d, 0xbb, 0x34, 0x46};
  int8_t snr[1] = {30};
  size_t n = buildTraceData(o, 0x11223344, 0, 2, h, snr, 4, -8);
  EXPECT_EQ("89000402" "44332211" "00000000" "2dbb3446" "1e" "f8", hex(o, n));
  EXPECT_EQ(12u + 4 + 1 + 1, n);
  // 2 one-byte hops
  uint8_t h2[2] = {0x2d, 0x5f};
  int8_t s2[2] = {4, -4};
  n = buildTraceData(o, 1, 2, 0, h2, s2, 2, 0);
  EXPECT_EQ("89000200" "01000000" "02000000" "2d5f" "04fc" "00", hex(o, n));
  // too long for a frame
  uint8_t big[200] = {0};
  EXPECT_EQ(0u, buildTraceData(o, 1, 2, 0, big, (int8_t*)big, 170, 0));
}

TEST(Trace, FormatSnr) {
  char b[16];
  formatSnr(b, sizeof b, 30);  EXPECT_STREQ("7.5", b);
  formatSnr(b, sizeof b, -14); EXPECT_STREQ("-3.5", b);
  formatSnr(b, sizeof b, 29);  EXPECT_STREQ("7.3", b);
  formatSnr(b, sizeof b, 0);   EXPECT_STREQ("0.0", b);
  formatSnr(b, sizeof b, -1);  EXPECT_STREQ("-0.3", b);
}

TEST(Trace, FormatResult) {
  char b[200];
  TraceResult r;
  formatTraceResult(b, sizeof b, r, 0);
  EXPECT_STREQ("> none", b);
  r.state = TraceResult::PENDING; r.tag = 0xDEADBEEF;
  formatTraceResult(b, sizeof b, r, 7);
  EXPECT_STREQ("> pending tag=DEADBEEF age=7s", b);
  r.state = TraceResult::TIMEOUT;
  formatTraceResult(b, sizeof b, r, 31);
  EXPECT_STREQ("> timeout (no reply after 30 s) tag=DEADBEEF", b);
  r.state = TraceResult::DONE; r.flags = 2; r.n_hops = 1;
  memcpy(r.hashes, "\x2d\xbb\x34\x46", 4); r.snr_x4[0] = 30; r.final_snr_x4 = -14; r.final_rssi = -93;
  formatTraceResult(b, sizeof b, r, 12);
  EXPECT_STREQ("> tag=DEADBEEF\n2dbb3446 7.5dB\nback -3.5dB rssi=-93", b);
  // worst case: 6 hops of 4 bytes, all negative, must fit the 150 byte CLI reply
  r.n_hops = 6; r.final_snr_x4 = -128; r.final_rssi = -127;
  for (int i = 0; i < 6; i++) { memcpy(r.hashes + 4 * i, "\xff\xff\xff\xff", 4); r.snr_x4[i] = -128; }
  size_t wn = formatTraceResult(b, 150, r, 99999);
  EXPECT_NE(nullptr, strstr(b, "back -32.0dB rssi=-127"));
  EXPECT_LT(wn, 149u);
  char small[24];
  size_t n = formatTraceResult(small, sizeof small, r, 12);   // truncates, stays terminated
  EXPECT_EQ(sizeof small - 1, n);
  EXPECT_EQ(0, small[n]);
}

TEST(Trace, FormatNames) {
  Contact c[4];
  bool used[4] = {true, false, true, true};
  memset(c, 0, sizeof c);
  c[0].pubkey[0] = 0x2d; c[0].pubkey[1] = 0xbb; c[0].pubkey[2] = 0x34; c[0].pubkey[3] = 0x46;
  strcpy(c[0].name, "Hill Repeater"); c[0].lastmod = 10;
  c[2].pubkey[0] = 0xaa; strcpy(c[2].name, "Newest"); c[2].lastmod = 30;
  c[3].pubkey[0] = 0xbb; strcpy(c[3].name, "Mid"); c[3].lastmod = 20;
  char b[160];
  formatNames(b, sizeof b, c, used, 4, 0);
  EXPECT_STREQ("aa000000 Newest\nbb000000 Mid\n2dbb3446 Hill Repeater", b);
  formatNames(b, sizeof b, c, used, 4, 2);
  EXPECT_STREQ("2dbb3446 Hill Repeater", b);
  formatNames(b, sizeof b, c, used, 4, 3);
  EXPECT_STREQ("> only 3 names", b);
  char tiny[50];   // two entries do not fit: hint to continue
  formatNames(tiny, sizeof tiny, c, used, 4, 0);
  EXPECT_NE(nullptr, strstr(tiny, "more: get names "));
  bool none[4] = {false, false, false, false};
  formatNames(b, sizeof b, c, none, 4, 0);
  EXPECT_STREQ("> no names heard yet", b);
}
