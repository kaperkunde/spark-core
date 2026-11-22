#include <gtest/gtest.h>
#include <string.h>
#include <string>
// Units under test are included as sources (see test_spark_hostlink for why).
#include "../../examples/spark_room/MemberDir.cpp"
#include "../../examples/spark_kids/KidsLogic.cpp"

using namespace spark;

static void key(uint8_t* k, uint8_t seed) { for (int i = 0; i < 32; i++) k[i] = (uint8_t)(seed + i); }

TEST(MemberDir, TouchFindRemove) {
  MemberDir d;
  uint8_t a[32], b[32];
  key(a, 0x10); key(b, 0x20);
  MemberEntry* ma = d.touch(a, 100, false);
  ASSERT_TRUE(ma);
  d.setName(ma, "Nova");
  MemberEntry* mb = d.touch(b, 200, true);
  d.setName(mb, "Papa");
  EXPECT_EQ(d.count(), 2);
  EXPECT_EQ(d.find(a), ma);
  EXPECT_EQ(d.findName("nova"), ma);
  EXPECT_EQ(d.findName("pa"), mb);          // unique prefix
  EXPECT_EQ(d.findHexPrefix("1011"), ma);
  EXPECT_EQ(d.findHexPrefix("zz"), nullptr);
  EXPECT_TRUE(mb->flags & MEMBER_FLAG_ADMIN);
  EXPECT_TRUE(d.touch(a, 150, false) == ma && d.count() == 2);
  EXPECT_TRUE(d.remove(d.find(a)));
  EXPECT_EQ(d.count(), 1);
  EXPECT_EQ(d.find(a), nullptr);
}

TEST(MemberDir, EvictsOldestNonAdminWhenFull) {
  MemberDir d;
  uint8_t k[32];
  for (int i = 0; i < MEMBER_DIR_MAX; i++) { key(k, (uint8_t)(i * 3)); d.touch(k, 1000 + i, i == 0); }
  key(k, 0xF0);
  MemberEntry* m = d.touch(k, 5000, false);
  ASSERT_TRUE(m);
  EXPECT_EQ(d.count(), MEMBER_DIR_MAX);
  uint8_t k0[32], k1[32];
  key(k0, 0); key(k1, 3);
  EXPECT_TRUE(d.find(k0));    // admin kept even though oldest
  EXPECT_FALSE(d.find(k1));   // oldest non-admin replaced
}

TEST(MemberDir, NameIsCutOnUtf8Boundary) {
  char out[MEMBER_NAME_MAX + 1];
  const char* s = "Abcdefghijklmn\xC3\xA9xyz";   // 'é' straddles byte 15
  memberCopyName(out, s, strlen(s));
  EXPECT_STREQ(out, "Abcdefghijklmn");
  memberCopyName(out, "No\nva  ", 7);
  EXPECT_STREQ(out, "Nova");
}

TEST(MemberDir, SerializeRoundTrip) {
  MemberDir d, e;
  uint8_t a[32]; key(a, 1);
  MemberEntry* m = d.touch(a, 1700000000, true);
  d.setName(m, "Ouder");
  d.setLocation(m, 12345670, 3456780, 1700000100, 9);
  uint8_t buf[MEMBER_FILE_MAX];
  size_t n = d.serialize(buf, sizeof(buf));
  ASSERT_EQ(n, 8 + MEMBER_REC_SIZE);
  ASSERT_TRUE(e.deserialize(buf, n));
  MemberEntry* r = e.find(a);
  ASSERT_TRUE(r);
  EXPECT_STREQ(r->name, "Ouder");
  EXPECT_EQ(r->lat, 12345670);
  EXPECT_EQ(r->lon, 3456780);
  EXPECT_EQ(r->fix_time, 1700000100u);
  EXPECT_EQ(r->sats, 9);
  EXPECT_EQ(r->flags, MEMBER_FLAG_ADMIN);
  buf[0] = 'X';
  EXPECT_FALSE(e.deserialize(buf, n));
  EXPECT_FALSE(e.deserialize(buf, 4));
}

TEST(MemberProto, PagesCoverEveryoneWithinLimit) {
  MemberDir d;
  uint8_t k[32];
  for (int i = 0; i < 7; i++) {
    key(k, (uint8_t)(i * 7));
    MemberEntry* m = d.touch(k, 10, i == 2);
    char nm[16]; snprintf(nm, sizeof(nm), "Kid%d-longname", i);
    d.setName(m, nm);
  }
  int start = 0, seen = 0;
  while (true) {
    uint8_t out[MEMBER_PAGE_MAX];
    size_t n = memberPageEncode(out, MEMBER_PAGE_MAX - 4, d.list(), d.count(), start);
    ASSERT_LE(n + 4, MEMBER_PAGE_MAX);
    uint8_t total, st;
    MemberPageItem items[8];
    int c = memberPageDecode(out, n, total, st, items, 8);
    ASSERT_GT(c, 0);
    EXPECT_EQ(total, 7);
    EXPECT_EQ(st, start);
    for (int i = 0; i < c; i++) {
      EXPECT_EQ(memcmp(items[i].key, d.list()[start + i].key, 32), 0);
      EXPECT_STREQ(items[i].name, d.list()[start + i].name);
      EXPECT_EQ(items[i].flags, (start + i) == 2 ? MEMBER_FLAG_ADMIN : 0);
    }
    seen += c;
    start += c;
    if (start >= total) break;
  }
  EXPECT_EQ(seen, 7);
}

TEST(MemberProto, DecodeRejectsTruncated) {
  uint8_t out[64] = { REQ_GET_MEMBERS, 1, 0, 1 };
  uint8_t total, st;
  MemberPageItem items[2];
  EXPECT_EQ(memberPageDecode(out, 10, total, st, items, 2), -1);
  EXPECT_EQ(memberPageDecode(out, 3, total, st, items, 2), -1);
  out[0] = 0xA1;
  EXPECT_EQ(memberPageDecode(out, 40, total, st, items, 2), -1);
}

TEST(MemberProto, RequestsAndLocation) {
  uint8_t b[32];
  size_t n = memberReqEncode(b, 3, "Nova");
  EXPECT_EQ(n, 7u);
  EXPECT_EQ(b[0], REQ_GET_MEMBERS);
  EXPECT_EQ(b[1], 3);
  EXPECT_EQ(memcmp(&b[2], "Nova", 5), 0);
  n = locationReqEncode(b, -33856000, 151215000, 1700000000, 7);
  EXPECT_EQ(n, 14u);
  int32_t la, lo; uint32_t t; uint8_t s;
  ASSERT_TRUE(locationReqDecode(b, n, la, lo, t, s));
  EXPECT_EQ(la, -33856000); EXPECT_EQ(lo, 151215000); EXPECT_EQ(t, 1700000000u); EXPECT_EQ(s, 7);
  locationReqEncode(b, 91000000, 0, 1, 0);
  EXPECT_FALSE(locationReqDecode(b, 14, la, lo, t, s));   // out of range
  locationReqEncode(b, 0, 0, 1, 0);
  EXPECT_FALSE(locationReqDecode(b, 14, la, lo, t, s));   // null island = no fix
}

TEST(MemberDir, CliFormatting) {
  MemberDir d;
  uint8_t k[32];
  key(k, 0xAB);
  MemberEntry* m = d.touch(k, 1000, false);
  d.setName(m, "Nova");
  key(k, 0x01);
  MemberEntry* p = d.touch(k, 1000, true);
  d.setName(p, "Papa");
  d.setLocation(m, 12345670, 3456780, 1000, 8);
  char out[160];
  d.formatWhere("", 1300, out, sizeof(out));
  EXPECT_STREQ(out, "Nova 5m 12.345670,3.456780 s8");
  d.formatWhere("papa", 1300, out, sizeof(out));
  EXPECT_STREQ(out, "Papa never");
  d.formatWhere("nobody", 1300, out, sizeof(out));
  EXPECT_STREQ(out, "Err - no member 'nobody'");
  d.formatMembers(0, 1000 + 7200, out, sizeof(out));
  EXPECT_STREQ(out, "ABACADAE Nova 2h\n01020304 Papa* 2h");
  // negative coordinates
  d.setLocation(m, -500000, -3456780, 2000, 3);
  d.formatWhere("nova", 2030, out, sizeof(out));
  EXPECT_STREQ(out, "Nova 30s -0.500000,-3.456780 s3");
}

TEST(MemberDir, CliPaging) {
  MemberDir d;
  uint8_t k[32];
  for (int i = 0; i < 12; i++) {
    key(k, (uint8_t)(i * 9));
    MemberEntry* m = d.touch(k, 100, false);
    char nm[16]; snprintf(nm, sizeof(nm), "Member%02d", i);
    d.setName(m, nm);
    d.setLocation(m, 52000000 + i, 4000000 + i, 100, 5);
  }
  char out[152];
  int total = 0, start = 0;
  for (int guard = 0; guard < 10; guard++) {
    int n = d.formatWhere(start ? std::to_string(start).c_str() : "", 200, out, sizeof(out));
    ASSERT_LT(n, 150);
    std::string s(out);
    size_t more = s.find("more: where ");
    std::string body = more == std::string::npos ? s : s.substr(0, more);
    for (char c : body) if (c == '\n') total++;
    if (more == std::string::npos) { total++; break; }
    start = atoi(s.c_str() + more + 12);
  }
  EXPECT_EQ(total, 12);
  int n = d.formatMembers(0, 200, out, sizeof(out));
  EXPECT_LT(n, 150);
  EXPECT_NE(std::string(out).find("more: members"), std::string::npos);
}

TEST(FormatAge, Units) {
  char b[12];
  formatAge(b, sizeof(b), 1000, 0); EXPECT_STREQ(b, "never");
  formatAge(b, sizeof(b), 1000, 990); EXPECT_STREQ(b, "10s");
  formatAge(b, sizeof(b), 100000, 100000 - 3 * 3600); EXPECT_STREQ(b, "3h");
  formatAge(b, sizeof(b), 1000000, 1000000 - 5 * 86400); EXPECT_STREQ(b, "5d");
  formatAge(b, sizeof(b), 1000, 2000); EXPECT_STREQ(b, "?");
}

// ---- kids firmware logic ----

TEST(Kids, FoldUtf8) {
  char out[64];
  kids::foldUtf8("Caf\xC3\xA9 \xC3\x9F \xE2\x80\x9Chi\xE2\x80\x9D", out, sizeof(out));
  EXPECT_STREQ(out, "Cafe ss \"hi\"");
  kids::foldUtf8("ok \xF0\x9F\x98\x80!", out, sizeof(out));   // emoji
  EXPECT_STREQ(out, "ok ?!");
  kids::foldUtf8("a\nb\x01" "c", out, sizeof(out));
  EXPECT_STREQ(out, "a bc");
  kids::foldUtf8("\xC3", out, sizeof(out));   // truncated sequence
  EXPECT_STREQ(out, "?");
  kids::foldUtf8("abcdef", out, 4);
  EXPECT_STREQ(out, "abc");
}

static int fixedW(char, void*) { return 10; }
struct Lines { std::string l[10]; int n = 0; };
static void addLine(const char* s, int len, void* ctx) { Lines* L = (Lines*)ctx; L->l[L->n++] = std::string(s, len); }

TEST(Kids, WrapText) {
  Lines L;
  int n = kids::wrapText("hello world this is", 60, fixedW, 0, addLine, &L);
  ASSERT_EQ(n, 4);
  EXPECT_EQ(L.l[0], "hello");
  EXPECT_EQ(L.l[1], "world");
  EXPECT_EQ(L.l[2], "this");
  EXPECT_EQ(L.l[3], "is");
  Lines M;
  n = kids::wrapText("abcdefghij", 40, fixedW, 0, addLine, &M);   // no space: hard break
  ASSERT_EQ(n, 3);
  EXPECT_EQ(M.l[0], "abcd");
  EXPECT_EQ(M.l[2], "ij");
  EXPECT_EQ(kids::wrapText("", 40, fixedW, 0, addLine, &M), 0);
}

TEST(Kids, LocationPolicy) {
  kids::LocState s = {};
  EXPECT_TRUE(kids::shouldSendLocation(s, 100, 52000000, 4000000));   // first fix
  s.tried_at = 100;
  EXPECT_FALSE(kids::shouldSendLocation(s, 130, 52000000, 4000000));  // waiting for reply
  EXPECT_TRUE(kids::shouldSendLocation(s, 161, 52000000, 4000000));   // retry
  s = {}; s.sent = true; s.sent_at = 1000; s.lat = 52000000; s.lon = 4000000;
  EXPECT_FALSE(kids::shouldSendLocation(s, 1000 + 4 * 60, 52010000, 4000000));   // moved, but < 5 min
  EXPECT_TRUE(kids::shouldSendLocation(s, 1000 + 5 * 60, 52010000, 4000000));    // moved ~1.1 km, 5 min
  EXPECT_FALSE(kids::shouldSendLocation(s, 1000 + 10 * 60, 52000300, 4000000));  // ~33 m: still
  EXPECT_TRUE(kids::shouldSendLocation(s, 1000 + 15 * 60, 52000300, 4000000));   // heartbeat
}

TEST(Kids, Distance) {
  // Amsterdam Dam -> Centraal is roughly 650 m
  uint32_t d = kids::distanceMeters(52373100, 4893100, 52378900, 4900400);
  EXPECT_GT(d, 750u);
  EXPECT_LT(d, 850u);
  EXPECT_EQ(kids::distanceMeters(1, 1, 1, 1), 0u);
}

TEST(Kids, Clock) {
  EXPECT_EQ(kids::bootClock(1715000000, 1795000000), 1795000000u);
  EXPECT_EQ(kids::bootClock(1796000000, 1795000000), 1796000000u);
  EXPECT_TRUE(kids::acceptRoomTime(1000, 2000, false));
  EXPECT_FALSE(kids::acceptRoomTime(3000, 2000, false));   // never backwards
  EXPECT_FALSE(kids::acceptRoomTime(1000, 2000, true));    // GPS wins
}

TEST(Kids, PosixTzCentralEurope) {
  kids::TzRule r;
  ASSERT_TRUE(kids::parseTz("CET-1CEST,M3.5.0,M10.5.0/3", r));
  EXPECT_EQ(kids::tzOffset(r, 1767225600), 3600);   // 2026-01-01
  EXPECT_EQ(kids::tzOffset(r, 1782864000), 7200);   // 2026-07-01
  // 2026: CEST from 29 Mar 01:00 UTC (1774746000) to 25 Oct 01:00 UTC (1792890000)
  EXPECT_EQ(kids::tzOffset(r, 1774746000 - 1), 3600);
  EXPECT_EQ(kids::tzOffset(r, 1774746000), 7200);
  EXPECT_EQ(kids::tzOffset(r, 1792890000 - 1), 7200);
  EXPECT_EQ(kids::tzOffset(r, 1792890000), 3600);
}

TEST(Kids, PosixTzOthers) {
  kids::TzRule r;
  // US Eastern 2026: EDT from 8 Mar 07:00 UTC (1772953200) to 1 Nov 06:00 UTC (1793512800)
  ASSERT_TRUE(kids::parseTz("EST5EDT,M3.2.0,M11.1.0", r));
  EXPECT_EQ(kids::tzOffset(r, 1772953200 - 1), -5 * 3600);
  EXPECT_EQ(kids::tzOffset(r, 1772953200), -4 * 3600);
  EXPECT_EQ(kids::tzOffset(r, 1793512800 - 1), -4 * 3600);
  EXPECT_EQ(kids::tzOffset(r, 1793512800), -5 * 3600);
  // southern hemisphere: Sydney is on daylight time in January, standard time in July
  ASSERT_TRUE(kids::parseTz("AEST-10AEDT,M10.1.0,M4.1.0/3", r));
  EXPECT_EQ(kids::tzOffset(r, 1767225600), 11 * 3600);
  EXPECT_EQ(kids::tzOffset(r, 1782864000), 10 * 3600);
  // fixed offsets, quoted names, minutes
  ASSERT_TRUE(kids::parseTz("UTC0", r));
  EXPECT_EQ(kids::tzOffset(r, 1782864000), 0);
  ASSERT_TRUE(kids::parseTz("<+0530>-5:30", r));
  EXPECT_EQ(kids::tzOffset(r, 1782864000), 5 * 3600 + 1800);
  // invalid strings fall back to UTC
  EXPECT_FALSE(kids::parseTz("", r));
  EXPECT_FALSE(kids::parseTz("Europe/Amsterdam", r));
  EXPECT_FALSE(kids::parseTz("CET-1CEST,M13.5.0,M10.5.0", r));
  EXPECT_EQ(kids::tzOffset(r, 1782864000), 0);
}

TEST(Kids, KeyAndMisc) {
  uint8_t k[32];
  EXPECT_TRUE(kids::parseKey("0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF", k));
  EXPECT_EQ(k[0], 0x01);
  EXPECT_EQ(k[31], 0xEF);
  EXPECT_FALSE(kids::parseKey("0123", k));
  EXPECT_FALSE(kids::parseKey("G123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF", k));
  char h[9];
  kids::toHex(h, k, 4);
  EXPECT_STREQ(h, "01234567");
  EXPECT_EQ(kids::batteryBars(4200), 4);
  EXPECT_EQ(kids::batteryBars(3600), 1);
  EXPECT_EQ(kids::batteryBars(3400), 0);
  int c = kids::authorColor(k, 8);
  EXPECT_GE(c, 0); EXPECT_LT(c, 8);
  EXPECT_EQ(c, kids::authorColor(k, 8));
}


TEST(KidsPublic, SettingFollowsBuildDefaultUntilToggled) {
  using namespace kids;
  // never touched by a parent: the build default (.env KIDS_PUBLIC_ENABLED) decides
  EXPECT_FALSE(publicEnabled(PUBLIC_DEFAULT, false));
  EXPECT_TRUE(publicEnabled(PUBLIC_DEFAULT, true));
  // a stored choice wins over the build default either way
  EXPECT_TRUE(publicEnabled(PUBLIC_ON, false));
  EXPECT_FALSE(publicEnabled(PUBLIC_OFF, true));
  // unknown stored values (older prefs, rsvd bytes) behave like "default"
  EXPECT_FALSE(publicEnabled(7, false));
  EXPECT_TRUE(publicEnabled(7, true));
}

TEST(KidsPublic, ToggleStoresTheOppositeOfWhatIsShown) {
  using namespace kids;
  EXPECT_EQ(publicToggled(PUBLIC_DEFAULT, false), PUBLIC_ON);
  EXPECT_EQ(publicToggled(PUBLIC_DEFAULT, true), PUBLIC_OFF);
  EXPECT_EQ(publicToggled(PUBLIC_ON, false), PUBLIC_OFF);
  EXPECT_EQ(publicToggled(PUBLIC_OFF, true), PUBLIC_ON);
  // toggling twice returns to the same visible state, and the value persists
  for (int def = 0; def < 2; def++) {
    uint8_t m = PUBLIC_DEFAULT;
    bool before = publicEnabled(m, def);
    m = publicToggled(m, def);
    EXPECT_NE(publicEnabled(m, def), before);
    m = publicToggled(m, def);
    EXPECT_EQ(publicEnabled(m, def), before);
    EXPECT_NE(m, PUBLIC_DEFAULT);   // an explicit choice, no longer following the build
  }
}

TEST(KidsPublic, RateLimitGapAndHourlyCap) {
  using namespace kids;
  PublicRate r;
  memset(&r, 0, sizeof(r));
  uint32_t wait = 99;
  EXPECT_TRUE(publicRateAllow(r, 1000, 3, &wait));
  EXPECT_EQ(wait, 0u);
  publicRateRecord(r, 1000);
  EXPECT_FALSE(publicRateAllow(r, 1010, 3, &wait));   // minimum gap
  EXPECT_EQ(wait, PUBLIC_MIN_GAP_SECS - 10);
  EXPECT_TRUE(publicRateAllow(r, 1000 + PUBLIC_MIN_GAP_SECS, 3, &wait));
  publicRateRecord(r, 1100);
  publicRateRecord(r, 1200);
  EXPECT_FALSE(publicRateAllow(r, 1300, 3, &wait));   // 3 in the last hour
  EXPECT_EQ(wait, 3600u - 300u);                        // until the 1000 post leaves the hour
  EXPECT_TRUE(publicRateAllow(r, 1000 + 3600, 3, &wait));
  EXPECT_TRUE(publicRateAllow(r, 1300, 4, nullptr));    // a higher cap allows it
  // out-of-range caps are clamped, never "unlimited"
  EXPECT_FALSE(publicRateAllow(r, 1300, 0, nullptr));
  PublicRate full;
  memset(&full, 0, sizeof(full));
  for (int i = 0; i < 40; i++) publicRateRecord(full, 100 + i * 60);
  EXPECT_FALSE(publicRateAllow(full, 100 + 40 * 60, 1000, nullptr));
}

TEST(KidsPublic, ChannelTextAndNameTag) {
  using namespace kids;
  char name[16];
  const char* t = splitChannelText("Papa: dinner is ready", name, sizeof(name));
  EXPECT_STREQ(name, "Papa");
  EXPECT_STREQ(t, "dinner is ready");
  t = splitChannelText("no name here", name, sizeof(name));
  EXPECT_STREQ(name, "");
  EXPECT_STREQ(t, "no name here");
  t = splitChannelText("AVeryLongNodeNameIndeed: hi", name, sizeof(name));
  EXPECT_STREQ(name, "AVeryLongNodeNa");   // cut to the buffer
  EXPECT_STREQ(t, "hi");
  t = splitChannelText(": hi", name, sizeof(name));
  EXPECT_STREQ(name, "");
  uint8_t a[4], b[4], c[4];
  nameTag("Papa", a); nameTag("Papa", b); nameTag("Mama", c);
  EXPECT_EQ(memcmp(a, b, 4), 0);
  EXPECT_NE(memcmp(a, c, 4), 0);
}

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
