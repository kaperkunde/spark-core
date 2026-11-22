#include "MemberGlue.h"

#ifdef ROOM_MEMBER_DIR

#include "../simple_room_server/MyMesh.h"
#include "MemberDir.h"

#define MEMBERS_FILE "/members"
#define SAVE_DELAY_MS 5000UL                  // membership or name changed
#define LOC_SAVE_DELAY_MS (15UL * 60 * 1000)  // only locations changed: spare the flash
#define CLI_REPLY_CAP 150                     // fits a LoRa CLI reply

static spark::MemberDir dir;
static unsigned long save_at = 0;
static uint8_t file_buf[spark::MEMBER_FILE_MAX];

class MemberDirAccess {
public:
  static FILESYSTEM* fs(MyMesh& m) { return m._fs; }
  static uint32_t now(MyMesh& m) { return m.getRTCClock()->getCurrentTime(); }
};

static void schedule() {
  unsigned long t;
  if (dir.dirty()) t = millis() + SAVE_DELAY_MS;
  else if (dir.locDirty()) t = millis() + LOC_SAVE_DELAY_MS;
  else return;
  if (save_at == 0 || (long)(t - save_at) < 0) save_at = t ? t : 1;
}

static void save(MyMesh& m) {
  FILESYSTEM* fs = MemberDirAccess::fs(m);
  size_t n = dir.serialize(file_buf, sizeof(file_buf));
  if (!fs || n == 0) return;
#if defined(NRF52_PLATFORM)
  fs->remove(MEMBERS_FILE);
  File f = fs->open(MEMBERS_FILE, FILE_O_WRITE);
#elif defined(RP2040_PLATFORM)
  File f = fs->open(MEMBERS_FILE, "w");
#else
  File f = fs->open(MEMBERS_FILE, "w", true);
#endif
  if (f) {
    f.write(file_buf, n);
    f.close();
    dir.clean();
  }
}

void member_dir_begin(MyMesh& m) {
  FILESYSTEM* fs = MemberDirAccess::fs(m);
  if (!fs || !fs->exists(MEMBERS_FILE)) return;
#if defined(RP2040_PLATFORM)
  File f = fs->open(MEMBERS_FILE, "r");
#else
  File f = fs->open(MEMBERS_FILE);
#endif
  if (!f) return;
  size_t n = f.read(file_buf, sizeof(file_buf));
  f.close();
  if (!dir.deserialize(file_buf, n)) dir.clear();
}

void member_dir_login(MyMesh& m, ClientInfo* c) {
  uint8_t role = c->permissions & PERM_ACL_ROLE_MASK;
  if (role != PERM_ACL_READ_WRITE && role != PERM_ACL_ADMIN) return;
  dir.touch(c->id.pub_key, MemberDirAccess::now(m), role == PERM_ACL_ADMIN);
  schedule();
}

int member_dir_request(MyMesh& m, ClientInfo* c, const uint8_t* p, size_t len, uint8_t* out, size_t cap) {
  if (len < 1 || (p[0] != spark::REQ_GET_MEMBERS && p[0] != spark::REQ_SET_LOCATION)) return 0;
  uint8_t role = c->permissions & PERM_ACL_ROLE_MASK;
  if (role != PERM_ACL_READ_WRITE && role != PERM_ACL_ADMIN) return 0;   // guests get nothing
  spark::MemberEntry* me = dir.touch(c->id.pub_key, MemberDirAccess::now(m), role == PERM_ACL_ADMIN);

  if (p[0] == spark::REQ_GET_MEMBERS) {
    uint8_t start = len >= 2 ? p[1] : 0;
    if (me && len > 2) {   // requester's own name (NUL / padding terminated)
      char name[spark::MEMBER_NAME_MAX + 1];
      spark::memberCopyName(name, (const char*)&p[2], len - 2);
      if (name[0] && strcmp(name, me->name) != 0) {
        dir.setName(me, name);
#ifdef WITH_HOSTLINK
        hostlink_note_name(me->key, me->name);   // the bridge and `get names` learn it at once
#endif
      }
    }
    schedule();
    size_t lim = spark::MEMBER_PAGE_MAX - 4;
    if (cap < lim) lim = cap;
    return (int)spark::memberPageEncode(out, lim, dir.list(), dir.count(), start);
  }

  int32_t lat, lon;
  uint32_t fix_time;
  uint8_t sats;
  out[0] = spark::REQ_SET_LOCATION;
  if (me && spark::locationReqDecode(p, len, lat, lon, fix_time, sats)) {
    dir.setLocation(me, lat, lon, fix_time, sats);
    out[1] = 0;
  } else {
    out[1] = 1;   // rejected (bad fix)
  }
  schedule();
  return 2;
}

bool member_dir_command(MyMesh& m, const char* cmd, char* reply) {
  uint32_t now = MemberDirAccess::now(m);
  if (strcmp(cmd, "members") == 0 || strncmp(cmd, "members ", 8) == 0) {
    dir.formatMembers(cmd[7] ? atoi(cmd + 8) : 0, now, reply, CLI_REPLY_CAP);
    return true;
  }
  if (strcmp(cmd, "where") == 0 || strncmp(cmd, "where ", 6) == 0) {
    dir.formatWhere(cmd[5] ? cmd + 6 : "", now, reply, CLI_REPLY_CAP);
    return true;
  }
  if (strncmp(cmd, "member del ", 11) == 0) {
    const char* a = cmd + 11;
    spark::MemberEntry* e = dir.findHexPrefix(a);
    if (!e) e = dir.findName(a);
    if (!e) { strcpy(reply, "Err - no unique member"); return true; }
    char nm[spark::MEMBER_NAME_MAX + 1];
    strcpy(nm, e->name);
    dir.remove(e);
    schedule();
    snprintf(reply, CLI_REPLY_CAP, "OK - removed %s", nm[0] ? nm : a);
    return true;
  }
  if (strncmp(cmd, "member name ", 12) == 0) {
    char hex[65];
    const char* a = cmd + 12;
    const char* sp = strchr(a, ' ');
    if (!sp || sp - a > 64 || !sp[1]) { strcpy(reply, "Err - member name <hex prefix> <name>"); return true; }
    memcpy(hex, a, sp - a);
    hex[sp - a] = 0;
    spark::MemberEntry* e = dir.findHexPrefix(hex);
    if (!e) { strcpy(reply, "Err - no unique member"); return true; }
    dir.setName(e, sp + 1);
#ifdef WITH_HOSTLINK
    hostlink_note_name(e->key, e->name);
#endif
    schedule();
    strcpy(reply, "OK");
    return true;
  }
  return false;
}

void member_dir_advert(MyMesh& m, const mesh::Identity& id, const uint8_t* app_data, size_t app_data_len) {
  spark::MemberEntry* e = dir.find(id.pub_key);
  if (!e) return;
  AdvertDataParser parser(app_data, (uint8_t)app_data_len);
  if (!parser.isValid() || !parser.hasName()) return;
  dir.setName(e, parser.getName());
  schedule();
}

void member_dir_loop(MyMesh& m) {
#ifdef WITH_HOSTLINK
  static bool seeded = false;   // after boot: give HostLink's name cache the directory names
  if (!seeded) {
    seeded = true;
    for (int i = 0; i < dir.count(); i++) {
      if (dir.at(i)->name[0]) hostlink_note_name(dir.at(i)->key, dir.at(i)->name);
    }
  }
#endif
  if (save_at && (long)(millis() - save_at) >= 0) {
    save_at = 0;
    save(m);
  }
}

#endif  // ROOM_MEMBER_DIR
