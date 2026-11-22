#include <gtest/gtest.h>
#include <string.h>
#include <string>
// Units under test are included as sources (see test_spark_hostlink for why).
#include "../../examples/spark_room/MemberDir.cpp"

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

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
