#include "MemberDir.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

namespace spark {

static void put32(uint8_t* p, uint32_t v) {
  p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}
static uint32_t get32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void memberCopyName(char* dest, const char* src, size_t src_len) {
  size_t n = 0;
  for (size_t i = 0; i < src_len && src[i]; i++) n = i + 1;
  if (n > (size_t)MEMBER_NAME_MAX) {
    n = MEMBER_NAME_MAX;
    // back off to a UTF-8 lead byte so a multi-byte character is never cut
    while (n > 0 && ((uint8_t)src[n] & 0xC0) == 0x80) n--;
  }
  size_t o = 0;
  for (size_t i = 0; i < n; i++) {
    uint8_t c = (uint8_t)src[i];
    if (c < 0x20 || c == 0x7F) continue;
    dest[o++] = (char)c;
  }
  // trim trailing spaces
  while (o > 0 && dest[o - 1] == ' ') o--;
  dest[o] = 0;
}

size_t memberPageEncode(uint8_t* out, size_t cap, const MemberEntry* list, int total, int start) {
  if (cap < 4) return 0;
  if (start < 0) start = 0;
  if (start > total) start = total;
  size_t i = 0;
  out[i++] = REQ_GET_MEMBERS;
  out[i++] = (uint8_t)total;
  out[i++] = (uint8_t)start;
  size_t count_at = i++;
  uint8_t count = 0;
  for (int k = start; k < total; k++) {
    const MemberEntry& m = list[k];
    size_t nl = strlen(m.name);
    if (i + 34 + nl > cap) break;
    memcpy(&out[i], m.key, 32); i += 32;
    out[i++] = m.flags & MEMBER_FLAG_ADMIN;
    out[i++] = (uint8_t)nl;
    memcpy(&out[i], m.name, nl); i += nl;
    count++;
  }
  out[count_at] = count;
  return i;
}

int memberPageDecode(const uint8_t* in, size_t len, uint8_t& total, uint8_t& start, MemberPageItem* items,
                     int max_items) {
  if (len < 4 || in[0] != REQ_GET_MEMBERS) return -1;
  total = in[1];
  start = in[2];
  int count = in[3];
  size_t i = 4;
  int n = 0;
  for (int k = 0; k < count; k++) {
    if (i + 34 > len) return -1;
    uint8_t nl = in[i + 33];
    if (i + 34 + nl > len) return -1;
    if (n < max_items) {
      memcpy(items[n].key, &in[i], 32);
      items[n].flags = in[i + 32];
      memberCopyName(items[n].name, (const char*)&in[i + 34], nl);
      n++;
    }
    i += 34 + nl;
  }
  return n;
}

size_t memberReqEncode(uint8_t* out, uint8_t start, const char* name) {
  size_t i = 0;
  out[i++] = REQ_GET_MEMBERS;
  out[i++] = start;
  if (name) {
    char tmp[MEMBER_NAME_MAX + 1];
    memberCopyName(tmp, name, strlen(name));
    size_t nl = strlen(tmp);
    memcpy(&out[i], tmp, nl);
    i += nl;
  }
  out[i++] = 0;
  return i;
}

size_t locationReqEncode(uint8_t* out, int32_t lat, int32_t lon, uint32_t fix_time, uint8_t sats) {
  out[0] = REQ_SET_LOCATION;
  put32(&out[1], (uint32_t)lat);
  put32(&out[5], (uint32_t)lon);
  put32(&out[9], fix_time);
  out[13] = sats;
  return 14;
}

bool locationReqDecode(const uint8_t* in, size_t len, int32_t& lat, int32_t& lon, uint32_t& fix_time,
                       uint8_t& sats) {
  if (len < 13 || in[0] != REQ_SET_LOCATION) return false;
  lat = (int32_t)get32(&in[1]);
  lon = (int32_t)get32(&in[5]);
  fix_time = get32(&in[9]);
  sats = len >= 14 ? in[13] : 0;
  if (lat < -90000000 || lat > 90000000 || lon < -180000000 || lon > 180000000) return false;
  if (lat == 0 && lon == 0) return false;
  return true;
}

// ---------------------------------------------------------------------------------

void MemberDir::clear() {
  memset(_e, 0, sizeof(_e));
  _n = 0;
  _dirty = _loc_dirty = false;
}

MemberEntry* MemberDir::find(const uint8_t* key) {
  for (int i = 0; i < _n; i++) if (memcmp(_e[i].key, key, 32) == 0) return &_e[i];
  return 0;
}

MemberEntry* MemberDir::findPrefix(const uint8_t* prefix, int len) {
  MemberEntry* hit = 0;
  for (int i = 0; i < _n; i++) {
    if (memcmp(_e[i].key, prefix, len) == 0) {
      if (hit) return 0;   // ambiguous
      hit = &_e[i];
    }
  }
  return hit;
}

static int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

MemberEntry* MemberDir::findHexPrefix(const char* hex) {
  uint8_t pre[32];
  int n = 0;
  while (hex[2 * n] && hex[2 * n + 1] && n < 32) {
    int a = hexval(hex[2 * n]), b = hexval(hex[2 * n + 1]);
    if (a < 0 || b < 0) return 0;
    pre[n++] = (uint8_t)(a * 16 + b);
  }
  if (n == 0 || (hex[2 * n] != 0 && hex[2 * n] != ' ')) return 0;
  return findPrefix(pre, n);
}

static int ci_cmp(const char* a, const char* b, size_t n) {
  for (size_t i = 0; i < n; i++) {
    char x = a[i], y = b[i];
    if (x >= 'A' && x <= 'Z') x += 32;
    if (y >= 'A' && y <= 'Z') y += 32;
    if (x != y) return 1;
    if (x == 0) return 0;
  }
  return 0;
}

MemberEntry* MemberDir::findName(const char* name) {
  size_t nl = strlen(name);
  if (nl == 0) return 0;
  for (int i = 0; i < _n; i++) {
    if (strlen(_e[i].name) == nl && ci_cmp(_e[i].name, name, nl) == 0) return &_e[i];
  }
  MemberEntry* hit = 0;
  for (int i = 0; i < _n; i++) {
    if (strlen(_e[i].name) >= nl && ci_cmp(_e[i].name, name, nl) == 0) {
      if (hit) return 0;
      hit = &_e[i];
    }
  }
  return hit;
}

MemberEntry* MemberDir::touch(const uint8_t* key, uint32_t now, bool admin) {
  MemberEntry* m = find(key);
  if (!m) {
    if (_n < MEMBER_DIR_MAX) {
      m = &_e[_n++];
    } else {
      int victim = -1;
      for (int i = 0; i < _n; i++) {
        if (_e[i].flags & MEMBER_FLAG_ADMIN) continue;
        if (victim < 0 || _e[i].last_seen < _e[victim].last_seen) victim = i;
      }
      if (victim < 0) return 0;
      m = &_e[victim];
    }
    memset(m, 0, sizeof(*m));
    memcpy(m->key, key, 32);
    _dirty = true;
  }
  uint8_t f = admin ? (m->flags | MEMBER_FLAG_ADMIN) : (m->flags & ~MEMBER_FLAG_ADMIN);
  if (f != m->flags) { m->flags = f; _dirty = true; }
  if (now > m->last_seen) {
    if (now - m->last_seen > 3600) _loc_dirty = true;   // persist last_seen at most lazily
    m->last_seen = now;
  }
  return m;
}

bool MemberDir::remove(MemberEntry* m) {
  int i = (int)(m - _e);
  if (i < 0 || i >= _n) return false;
  for (int k = i; k < _n - 1; k++) _e[k] = _e[k + 1];
  _n--;
  memset(&_e[_n], 0, sizeof(_e[_n]));
  _dirty = true;
  return true;
}

void MemberDir::setName(MemberEntry* m, const char* name) {
  char tmp[MEMBER_NAME_MAX + 1];
  memberCopyName(tmp, name, strlen(name));
  if (tmp[0] == 0 || strcmp(tmp, m->name) == 0) return;
  strcpy(m->name, tmp);
  _dirty = true;
}

void MemberDir::setLocation(MemberEntry* m, int32_t lat, int32_t lon, uint32_t fix_time, uint8_t sats) {
  if (fix_time < m->fix_time) return;   // older than what we have
  m->lat = lat;
  m->lon = lon;
  m->fix_time = fix_time;
  m->sats = sats;
  _loc_dirty = true;
}

size_t MemberDir::serialize(uint8_t* out, size_t cap) const {
  size_t need = 8 + (size_t)_n * MEMBER_REC_SIZE;
  if (cap < need) return 0;
  memcpy(out, "SMD1", 4);
  out[4] = 1;
  out[5] = (uint8_t)_n;
  out[6] = out[7] = 0;
  size_t i = 8;
  for (int k = 0; k < _n; k++) {
    const MemberEntry& m = _e[k];
    memcpy(&out[i], m.key, 32); i += 32;
    memset(&out[i], 0, 16);
    memcpy(&out[i], m.name, strlen(m.name)); i += 16;
    out[i++] = m.flags;
    put32(&out[i], m.last_seen); i += 4;
    put32(&out[i], (uint32_t)m.lat); i += 4;
    put32(&out[i], (uint32_t)m.lon); i += 4;
    put32(&out[i], m.fix_time); i += 4;
    out[i++] = m.sats;
  }
  return i;
}

bool MemberDir::deserialize(const uint8_t* in, size_t len) {
  if (len < 8 || memcmp(in, "SMD1", 4) != 0 || in[4] != 1) return false;
  int n = in[5];
  if (n > MEMBER_DIR_MAX || len < 8 + (size_t)n * MEMBER_REC_SIZE) return false;
  clear();
  size_t i = 8;
  for (int k = 0; k < n; k++) {
    MemberEntry& m = _e[k];
    memcpy(m.key, &in[i], 32); i += 32;
    memberCopyName(m.name, (const char*)&in[i], 16); i += 16;
    m.flags = in[i++];
    m.last_seen = get32(&in[i]); i += 4;
    m.lat = (int32_t)get32(&in[i]); i += 4;
    m.lon = (int32_t)get32(&in[i]); i += 4;
    m.fix_time = get32(&in[i]); i += 4;
    m.sats = in[i++];
  }
  _n = n;
  return true;
}

void formatAge(char* out, size_t cap, uint32_t now, uint32_t t) {
  if (t == 0) { snprintf(out, cap, "never"); return; }
  if (t > now + 60) { snprintf(out, cap, "?"); return; }
  uint32_t d = t > now ? 0 : now - t;
  if (d < 60) snprintf(out, cap, "%us", (unsigned)d);
  else if (d < 3600) snprintf(out, cap, "%um", (unsigned)(d / 60));
  else if (d < 86400 * 2) snprintf(out, cap, "%uh", (unsigned)(d / 3600));
  else snprintf(out, cap, "%ud", (unsigned)(d / 86400));
}

static int fmt_coord(char* out, size_t cap, int32_t v) {
  long a = v < 0 ? -(long)v : (long)v;
  return snprintf(out, cap, "%s%ld.%06ld", v < 0 ? "-" : "", a / 1000000L, a % 1000000L);
}

static const char* display_name(const MemberEntry& m, char* buf) {
  if (m.name[0]) return m.name;
  snprintf(buf, 10, "%02X%02X%02X%02X", m.key[0], m.key[1], m.key[2], m.key[3]);
  return buf;
}

int MemberDir::formatMembers(int start, uint32_t now, char* out, size_t cap) const {
  size_t o = 0;
  out[0] = 0;
  if (_n == 0) return snprintf(out, cap, "no members");
  if (start < 0) start = 0;
  int k;
  for (k = start; k < _n; k++) {
    const MemberEntry& m = _e[k];
    char age[12], line[48];
    formatAge(age, sizeof(age), now, m.last_seen);
    int l = snprintf(line, sizeof(line), "%02X%02X%02X%02X %s%s %s\n", m.key[0], m.key[1], m.key[2], m.key[3],
                     m.name[0] ? m.name : "-", (m.flags & MEMBER_FLAG_ADMIN) ? "*" : "", age);
    if (o + l + 20 > cap) break;   // keep room for the "more:" line
    memcpy(&out[o], line, l);
    o += l;
  }
  if (k < _n) o += snprintf(&out[o], cap - o, "more: members %d", k);
  else if (o > 0) o--;   // drop the last newline
  out[o] = 0;
  return (int)o;
}

int MemberDir::formatWhere(const char* arg, uint32_t now, char* out, size_t cap) {
  while (arg && *arg == ' ') arg++;
  int start = 0;
  const MemberEntry* only = 0;
  if (arg && *arg) {
    if (*arg >= '0' && *arg <= '9') {
      start = atoi(arg);
    } else {
      only = findName(arg);
      if (!only) only = findHexPrefix(arg);
      if (!only) return snprintf(out, cap, "Err - no member '%s'", arg);
    }
  }
  size_t o = 0;
  out[0] = 0;
  int shown = 0;
  int k = only ? (int)(only - _e) : start;
  int end = only ? k + 1 : _n;
  for (; k < end; k++) {
    const MemberEntry& m = _e[k];
    char nb[10], line[72];
    int l;
    if (m.fix_time == 0) {
      if (!only) continue;
      l = snprintf(line, sizeof(line), "%s never\n", display_name(m, nb));
    } else {
      char age[12], la[16], lo[16];
      formatAge(age, sizeof(age), now, m.fix_time);
      fmt_coord(la, sizeof(la), m.lat);
      fmt_coord(lo, sizeof(lo), m.lon);
      l = snprintf(line, sizeof(line), "%s %s %s,%s s%u\n", display_name(m, nb), age, la, lo, (unsigned)m.sats);
    }
    if (o + l + 16 > cap) break;
    memcpy(&out[o], line, l);
    o += l;
    shown++;
  }
  if (!only && k < end) {
    o += snprintf(&out[o], cap - o, "more: where %d", k);
  } else if (o > 0) {
    o--;
  } else {
    o = snprintf(out, cap, "no locations");
  }
  out[o] = 0;
  return (int)o;
}

}  // namespace spark
