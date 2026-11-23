#pragma once

// SpArkNode member directory (ROOM_MEMBER_DIR) and its wire protocol.
//
// Pure C++ (no Arduino), shared by the room server (examples/spark_room) and the
// kids' client (examples/spark_kids), unit tested in test/test_spark_members.
//
// The room server remembers every client that logged in with the room or admin password:
// full public key, display name, last seen, and the latest GPS fix it reported.
//
// Requests (PAYLOAD_TYPE_REQ, after the 4-byte tag that every request starts with):
//
//   GET_MEMBERS  0xA0  u8 start, then optional requester name (UTF-8, up to 15 bytes,
//                      NUL or end of data terminates). Requires read/write or admin.
//                      The name, if present, becomes the requester's directory name.
//     reply:     u32 tag (reflected), u8 0xA0, u8 total, u8 start, u8 count,
//                then `count` entries: 32-byte pubkey, u8 flags (bit 0 admin),
//                u8 name_len, name[name_len]. A reply never exceeds MEMBER_PAGE_MAX bytes.
//                The client asks again with start += count until start >= total.
//
//   SET_LOCATION 0xA1  i32 lat x1e6, i32 lon x1e6, u32 fix time (epoch secs, GPS clock),
//                      u8 satellites (0 = unknown). Requires read/write or admin.
//     reply:     u32 tag, u8 0xA1, u8 status (0 = stored).
//
// Locations are only readable through the admin CLI (`where`), never by other clients.

#include <stdint.h>
#include <stddef.h>

namespace spark {

static const uint8_t REQ_GET_MEMBERS = 0xA0;
static const uint8_t REQ_SET_LOCATION = 0xA1;

#ifndef MEMBER_DIR_MAX
#define MEMBER_DIR_MAX 32
#endif
static const int MEMBER_NAME_MAX = 15;         // bytes, without NUL
static const size_t MEMBER_PAGE_MAX = 120;     // reply bytes incl. tag (fits a flood path return)
static const uint8_t MEMBER_FLAG_ADMIN = 1;

struct MemberEntry {
  uint8_t key[32];
  char name[MEMBER_NAME_MAX + 1];
  uint8_t flags;
  uint32_t last_seen;      // room clock
  int32_t lat, lon;        // x1e6
  uint32_t fix_time;       // 0 = no fix known
  uint8_t sats;
};

// --- GET_MEMBERS page codec ---------------------------------------------------------

// Encodes the reply after the tag into out (starting with the 0xA0 byte); returns bytes written.
// `cap` is the space available after the tag.
size_t memberPageEncode(uint8_t* out, size_t cap, const MemberEntry* list, int total, int start);

struct MemberPageItem {
  uint8_t key[32];
  uint8_t flags;
  char name[MEMBER_NAME_MAX + 1];
};
// Decodes a reply (after the tag). Returns number of items, or -1 if malformed.
int memberPageDecode(const uint8_t* in, size_t len, uint8_t& total, uint8_t& start, MemberPageItem* items,
                     int max_items);

// Request bodies (what follows the tag). Return bytes written.
size_t memberReqEncode(uint8_t* out, uint8_t start, const char* name);
size_t locationReqEncode(uint8_t* out, int32_t lat, int32_t lon, uint32_t fix_time, uint8_t sats);
bool locationReqDecode(const uint8_t* in, size_t len, int32_t& lat, int32_t& lon, uint32_t& fix_time,
                       uint8_t& sats);

// --- directory ---------------------------------------------------------------------

// File format "SMD1", u8 version=1, u8 count, u16 0, then count records of MEMBER_REC_SIZE:
//   32 key, 16 name (NUL padded), u8 flags, u32 last_seen, i32 lat, i32 lon, u32 fix_time, u8 sats
static const size_t MEMBER_REC_SIZE = 32 + 16 + 1 + 4 + 4 + 4 + 4 + 1;
static const size_t MEMBER_FILE_MAX = 8 + MEMBER_DIR_MAX * MEMBER_REC_SIZE;

class MemberDir {
  MemberEntry _e[MEMBER_DIR_MAX];
  int _n;
  bool _dirty;      // membership/name change: save soon
  bool _loc_dirty;  // only a location changed: save lazily
public:
  MemberDir() { clear(); }
  void clear();

  int count() const { return _n; }
  const MemberEntry* list() const { return _e; }
  MemberEntry* at(int i) { return (i >= 0 && i < _n) ? &_e[i] : 0; }

  MemberEntry* find(const uint8_t* key);
  MemberEntry* findPrefix(const uint8_t* prefix, int len);     // unique match only
  MemberEntry* findHexPrefix(const char* hex);                  // 2..64 hex digits, unique match
  MemberEntry* findName(const char* name);                       // case-insensitive, exact, then prefix

  // Adds or refreshes a member. When full, the least recently seen non-admin entry is replaced.
  MemberEntry* touch(const uint8_t* key, uint32_t now, bool admin);
  bool remove(MemberEntry* m);
  void setName(MemberEntry* m, const char* name);
  void setLocation(MemberEntry* m, int32_t lat, int32_t lon, uint32_t fix_time, uint8_t sats);

  bool dirty() const { return _dirty; }
  bool locDirty() const { return _loc_dirty; }
  void clean() { _dirty = _loc_dirty = false; }

  size_t serialize(uint8_t* out, size_t cap) const;
  bool deserialize(const uint8_t* in, size_t len);

  // CLI text (each call fits a ~150 byte LoRa CLI reply).
  // `members [start]`: "<8 hex> <name> <age>" lines, "more: members N" when cut.
  int formatMembers(int start, uint32_t now, char* out, size_t cap) const;
  // `where [name|start]`: "<name> <age> <lat>,<lon> s<sats>" for members with a fix.
  int formatWhere(const char* arg, uint32_t now, char* out, size_t cap);
};

// "45s", "12m", "3h", "4d", "never" (t == 0) or "?" (t in the future by > 60 s)
void formatAge(char* out, size_t cap, uint32_t now, uint32_t t);
// Copies at most MEMBER_NAME_MAX bytes on a UTF-8 boundary, drops control characters.
void memberCopyName(char* dest, const char* src, size_t src_len);

}  // namespace spark
