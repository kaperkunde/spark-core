#include "KidsLogic.h"

#include <string.h>
#include <math.h>

namespace kids {

// Latin-1 0xC0..0xFF folded to ASCII (one or two chars)
static const char* const LATIN1_FOLD[64] = {
  "A", "A", "A", "A", "A", "A", "AE", "C", "E", "E", "E", "E", "I", "I", "I", "I",
  "D", "N", "O", "O", "O", "O", "O", "x", "O", "U", "U", "U", "U", "Y", "Th", "ss",
  "a", "a", "a", "a", "a", "a", "ae", "c", "e", "e", "e", "e", "i", "i", "i", "i",
  "d", "n", "o", "o", "o", "o", "o", "/", "o", "u", "u", "u", "u", "y", "th", "y",
};

static const char* foldCodepoint(uint32_t cp) {
  if (cp >= 0xC0 && cp <= 0xFF) return LATIN1_FOLD[cp - 0xC0];
  switch (cp) {
    case 0xA0: return " ";
    case 0xA9: return "(c)";
    case 0xB0: return "o";
    case 0x2018: case 0x2019: case 0x201A: return "'";
    case 0x201C: case 0x201D: case 0x201E: return "\"";
    case 0x2013: case 0x2014: return "-";
    case 0x2026: return "...";
    case 0x20AC: return "EUR";
    case 0x2764: return "<3";
    case 0x0152: return "OE";
    case 0x0153: return "oe";
    case 0x0160: return "S";
    case 0x0161: return "s";
    case 0x017D: return "Z";
    case 0x017E: return "z";
    case 0x0178: return "Y";
    case 0xFE0F: case 0x200D: return "";   // emoji variation selector / joiner
  }
  return "?";
}

size_t foldUtf8(const char* in, char* out, size_t cap) {
  size_t o = 0;
  if (cap == 0) return 0;
  const uint8_t* p = (const uint8_t*)in;
  while (*p) {
    uint32_t cp;
    int extra;
    uint8_t c = *p;
    if (c < 0x80) { cp = c; extra = 0; }
    else if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; extra = 1; }
    else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; extra = 2; }
    else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; extra = 3; }
    else { cp = '?'; extra = 0; }   // stray continuation byte
    p++;
    bool bad = false;
    for (int i = 0; i < extra; i++) {
      if ((*p & 0xC0) != 0x80) { bad = true; break; }
      cp = (cp << 6) | (*p & 0x3F);
      p++;
    }
    const char* s;
    char one[2];
    if (bad) s = "?";
    else if (extra == 0) {
      if (cp == '\n' || cp == '\r' || cp == '\t') cp = ' ';
      if (cp < 0x20 || cp == 0x7F) continue;
      one[0] = (char)cp; one[1] = 0; s = one;
    } else s = foldCodepoint(cp);
    size_t l = strlen(s);
    if (o + l >= cap) break;
    memcpy(&out[o], s, l);
    o += l;
  }
  out[o] = 0;
  return o;
}

int wrapText(const char* text, int max_w, CharWidthFn cw, void* cw_ctx, LineFn line, void* line_ctx) {
  int lines = 0;
  const char* p = text;
  while (*p) {
    while (*p == ' ') p++;
    if (!*p) break;
    int w = 0;
    const char* q = p;
    const char* last_space = 0;
    while (*q) {
      int cwid = cw(*q, cw_ctx);
      if (w + cwid > max_w && q > p) break;
      if (*q == ' ') last_space = q;
      w += cwid;
      q++;
    }
    const char* end = q;
    if (*q && last_space && last_space > p) end = last_space;   // break at the last space
    int len = (int)(end - p);
    while (len > 0 && p[len - 1] == ' ') len--;
    if (line) line(p, len, line_ctx);
    lines++;
    p = end;
  }
  return lines;
}

uint32_t distanceMeters(int32_t lat1, int32_t lon1, int32_t lat2, int32_t lon2) {
  const double R = 6371000.0;
  const double k = 3.14159265358979 / 180.0 / 1e6;
  double phi = (lat1 + lat2) / 2.0 * k;
  double dx = (double)(lon2 - lon1) * k * cos(phi);
  double dy = (double)(lat2 - lat1) * k;
  return (uint32_t)(R * sqrt(dx * dx + dy * dy));
}

bool shouldSendLocation(const LocState& s, uint32_t now, int32_t lat, int32_t lon) {
  if (s.tried_at && now - s.tried_at < LOC_RETRY_SECS) return false;   // waiting for a reply
  if (!s.sent) return true;
  uint32_t since = now - s.sent_at;
  if (since >= LOC_STILL_SECS) return true;
  if (since >= LOC_MOVING_SECS && distanceMeters(s.lat, s.lon, lat, lon) >= LOC_MOVE_METERS) return true;
  return false;
}

uint32_t bootClock(uint32_t rtc_now, uint32_t saved_floor) {
  return saved_floor > rtc_now ? saved_floor : rtc_now;
}

bool acceptRoomTime(uint32_t now, uint32_t room_time, bool gps_time_recent) {
  if (gps_time_recent) return false;
  return room_time > now;
}

static uint32_t days_from_civil(int y, unsigned m, unsigned d) {
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return (uint32_t)(era * 146097 + (int)doe - 719468);
}

static bool isLeap(int y) { return (y % 4 == 0 && y % 100 != 0) || y % 400 == 0; }

// --- POSIX TZ -------------------------------------------------------------------------

static const char* tzName(const char* p) {   // "CET" or "<+0530>"
  if (*p == '<') { while (*p && *p != '>') p++; return *p ? p + 1 : NULL; }
  const char* s = p;
  while ((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z')) p++;
  return p - s >= 3 ? p : NULL;
}

static const char* tzNum(const char* p, int* v) {
  if (*p < '0' || *p > '9') return NULL;
  int n = 0;
  while (*p >= '0' && *p <= '9') n = n * 10 + (*p++ - '0');
  *v = n;
  return p;
}

static const char* tzTime(const char* p, int32_t* secs) {   // [+-]hh[:mm[:ss]]
  int sign = 1, h = 0, m = 0, sec = 0;
  if (*p == '+' || *p == '-') { if (*p == '-') sign = -1; p++; }
  if (!(p = tzNum(p, &h)) || h > 167) return NULL;
  if (*p == ':') { if (!(p = tzNum(p + 1, &m)) || m > 59) return NULL;
    if (*p == ':') { if (!(p = tzNum(p + 1, &sec)) || sec > 59) return NULL; } }
  *secs = sign * (h * 3600 + m * 60 + sec);
  return p;
}

static const char* tzWhen(const char* p, TzRule::When* w) {
  int a = 0, b = 0, c = 0;
  if (*p == 'M') {
    if (!(p = tzNum(p + 1, &a)) || *p != '.' || !(p = tzNum(p + 1, &b)) || *p != '.' || !(p = tzNum(p + 1, &c))) return NULL;
    if (a < 1 || a > 12 || b < 1 || b > 5 || c > 6) return NULL;
    w->kind = 0; w->m = (uint16_t)a; w->w = (uint16_t)b; w->d = (uint16_t)c;
  } else if (*p == 'J') {
    if (!(p = tzNum(p + 1, &a)) || a < 1 || a > 365) return NULL;
    w->kind = 1; w->d = (uint16_t)a;
  } else {
    if (!(p = tzNum(p, &a)) || a > 365) return NULL;
    w->kind = 2; w->d = (uint16_t)a;
  }
  w->secs = 7200;   // default 02:00 local
  if (*p == '/' && !(p = tzTime(p + 1, &w->secs))) return NULL;
  return p;
}

bool parseTz(const char* tz, TzRule& r) {
  memset(&r, 0, sizeof(r));
  if (!tz) return false;
  const char* p = tzName(tz);
  int32_t off;
  if (!p || !(p = tzTime(p, &off))) { memset(&r, 0, sizeof(r)); return false; }
  r.std_off = -off;
  r.dst_off = r.std_off;
  if (*p == 0) return true;
  if (!(p = tzName(p))) { memset(&r, 0, sizeof(r)); return false; }
  r.dst_off = r.std_off + 3600;
  if (*p && *p != ',') {
    if (!(p = tzTime(p, &off))) { memset(&r, 0, sizeof(r)); return false; }
    r.dst_off = -off;
  }
  if (*p == 0) {   // DST named but no rule: the US rules are the POSIX default
    r.start = { 0, 3, 2, 0, 7200 };
    r.end = { 0, 11, 1, 0, 7200 };
  } else if (*p != ',' || !(p = tzWhen(p + 1, &r.start)) || *p != ',' || !(p = tzWhen(p + 1, &r.end)) || *p) {
    memset(&r, 0, sizeof(r));
    return false;
  }
  r.has_dst = true;
  return true;
}

// days since the epoch of a rule's date in year y
static int32_t tzDay(const TzRule::When& w, int y) {
  int32_t jan1 = (int32_t)days_from_civil(y, 1, 1);
  if (w.kind == 1) return jan1 + w.d - 1 + ((isLeap(y) && w.d >= 60) ? 1 : 0);
  if (w.kind == 2) return jan1 + w.d;
  int32_t first = (int32_t)days_from_civil(y, w.m, 1);
  int32_t next = w.m == 12 ? (int32_t)days_from_civil(y + 1, 1, 1) : (int32_t)days_from_civil(y, w.m + 1, 1);
  int wd1 = (int)((first + 4) % 7);   // 1970-01-01 was a Thursday; 0 = Sunday
  int32_t day = first + (w.d - wd1 + 7) % 7 + (w.w - 1) * 7;
  while (day >= next) day -= 7;   // week 5 = the last one
  return day;
}

int32_t tzOffset(const TzRule& r, uint32_t utc) {
  if (!r.has_dst) return r.std_off;
  int64_t local = (int64_t)utc + r.std_off;
  int y = 1970 + (int)(local / 86400 / 366);
  while ((int64_t)days_from_civil(y + 1, 1, 1) * 86400 <= local) y++;
  // start is given in standard time, end in daylight time
  int64_t start = (int64_t)tzDay(r.start, y) * 86400 + r.start.secs - r.std_off;
  int64_t end = (int64_t)tzDay(r.end, y) * 86400 + r.end.secs - r.dst_off;
  bool dst = start < end ? ((int64_t)utc >= start && (int64_t)utc < end)
                         : !((int64_t)utc >= end && (int64_t)utc < start);   // southern hemisphere
  return dst ? r.dst_off : r.std_off;
}

int batteryBars(uint16_t mv) {
  if (mv >= 4000) return 4;
  if (mv >= 3850) return 3;
  if (mv >= 3700) return 2;
  if (mv >= 3550) return 1;
  return 0;
}

int authorColor(const uint8_t* prefix, int n) {
  uint32_t h = 2166136261u;
  for (int i = 0; i < 4; i++) { h ^= prefix[i]; h *= 16777619u; }
  return (int)(h % (uint32_t)n);
}

void toHex(char* out, const uint8_t* in, int n) {
  static const char* d = "0123456789ABCDEF";
  for (int i = 0; i < n; i++) { out[2 * i] = d[in[i] >> 4]; out[2 * i + 1] = d[in[i] & 15]; }
  out[2 * n] = 0;
}

bool parseKey(const char* hex, uint8_t* key) {
  int n = 0, hi = -1;
  for (const char* p = hex; *p; p++) {
    char c = *p;
    int v;
    if (c == ' ' || c == ':') continue;
    if (c >= '0' && c <= '9') v = c - '0';
    else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
    else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
    else return false;
    if (hi < 0) hi = v;
    else {
      if (n >= 32) return false;
      key[n++] = (uint8_t)(hi * 16 + v);
      hi = -1;
    }
  }
  return n == 32 && hi < 0;
}

// --- Public channel -------------------------------------------------------------------

bool publicEnabled(uint8_t mode, bool build_default) {
  if (mode == PUBLIC_ON) return true;
  if (mode == PUBLIC_OFF) return false;
  return build_default;
}

uint8_t publicToggled(uint8_t mode, bool build_default) {
  return publicEnabled(mode, build_default) ? PUBLIC_OFF : PUBLIC_ON;
}

bool publicRateAllow(const PublicRate& r, uint32_t now, int per_hour, uint32_t* wait_secs) {
  if (per_hour < 1) per_hour = 1;
  if (per_hour > PUBLIC_RATE_SLOTS) per_hour = PUBLIC_RATE_SLOTS;
  uint32_t wait = 0;
  int in_hour = 0;
  uint32_t oldest = 0;   // oldest post time inside the hour
  for (int i = 0; i < PUBLIC_RATE_SLOTS; i++) {
    uint32_t t = r.sent[i];
    if (t == 0 || t > now) continue;
    uint32_t age = now - t;
    if (age < PUBLIC_MIN_GAP_SECS && PUBLIC_MIN_GAP_SECS - age > wait) wait = PUBLIC_MIN_GAP_SECS - age;
    if (age < 3600) {
      in_hour++;
      if (oldest == 0 || t < oldest) oldest = t;
    }
  }
  if (in_hour >= per_hour) {
    // the window frees up when enough old posts leave it; approximate by the oldest one
    uint32_t w = 3600 - (now - oldest);
    if (w > wait) wait = w;
  }
  if (wait_secs) *wait_secs = wait;
  return wait == 0;
}

void publicRateRecord(PublicRate& r, uint32_t now) {
  r.sent[r.next % PUBLIC_RATE_SLOTS] = now ? now : 1;
  r.next = (uint8_t)((r.next + 1) % PUBLIC_RATE_SLOTS);
}

const char* splitChannelText(const char* text, char* name, size_t cap) {
  if (cap) name[0] = 0;
  for (int i = 0; i < 32 && text[i]; i++) {
    if (text[i] == ':' && text[i + 1] == ' ') {
      if (i == 0) break;
      size_t n = (size_t)i < cap - 1 ? (size_t)i : cap - 1;
      memcpy(name, text, n);
      name[n] = 0;
      return text + i + 2;
    }
  }
  return text;
}

void nameTag(const char* name, uint8_t* tag4) {
  uint32_t h = 2166136261u;   // FNV-1a
  for (const char* p = name; *p; p++) { h ^= (uint8_t)*p; h *= 16777619u; }
  for (int i = 0; i < 4; i++) tag4[i] = (uint8_t)(h >> (8 * i));
  if (tag4[0] == 0 && tag4[1] == 0 && tag4[2] == 0 && tag4[3] == 0) tag4[0] = 1;
}

}  // namespace kids
