#include "PostStore.h"
#include <string.h>

namespace spark {

static inline void put32(uint8_t* p, uint32_t v) {
  p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; p[2] = (v >> 16) & 0xFF; p[3] = v >> 24;
}
static inline uint32_t get32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void postEncodeHeader(uint8_t* out, uint8_t count, uint32_t last_known_time) {
  memcpy(out, "SPP1", 4);
  out[4] = POSTFILE_VERSION;
  out[5] = count;
  out[6] = out[7] = 0;
  put32(&out[8], last_known_time);
}

bool postDecodeHeader(const uint8_t* in, size_t len, uint8_t& count, uint32_t& last_known_time) {
  if (len < POSTFILE_HEADER_SIZE || memcmp(in, "SPP1", 4) != 0 || in[4] != POSTFILE_VERSION) return false;
  count = in[5];
  last_known_time = get32(&in[8]);
  return true;
}

size_t postEncodeRec(uint8_t* out, const PostRec& r) {
  size_t tl = r.text_len > POST_MAX_TEXT ? POST_MAX_TEXT : r.text_len;
  put32(out, r.timestamp);
  memcpy(&out[4], r.author, 32);
  out[36] = (uint8_t)tl;
  memcpy(&out[37], r.text, tl);
  return POST_REC_FIXED + tl;
}

size_t postDecodeRec(const uint8_t* in, size_t len, PostRec& r) {
  if (len < POST_REC_FIXED) return 0;
  size_t tl = in[36];
  if (tl > POST_MAX_TEXT || len < POST_REC_FIXED + tl) return 0;
  r.timestamp = get32(in);
  r.flags = 0;
  memcpy(r.author, &in[4], 32);
  r.text_len = (uint8_t)tl;
  memcpy(r.text, &in[37], tl);
  r.text[tl] = 0;
  return POST_REC_FIXED + tl;
}

uint32_t epochFromCDate(const char* d) {
  static const char* months = "JanFebMarAprMayJunJulAugSepOctNovDec";
  if (!d || strlen(d) < 11) return 0;
  int m = -1;
  for (int i = 0; i < 12; i++) if (memcmp(d, months + 3 * i, 3) == 0) m = i + 1;
  if (m < 0) return 0;
  int day = (d[4] == ' ' ? 0 : (d[4] - '0') * 10) + (d[5] - '0');
  int y = (d[7] - '0') * 1000 + (d[8] - '0') * 100 + (d[9] - '0') * 10 + (d[10] - '0');
  if (day < 1 || day > 31 || y < 2000) return 0;
  // days from civil (Howard Hinnant)
  int yy = y - (m <= 2 ? 1 : 0);
  int era = yy / 400;
  int yoe = yy - era * 400;
  int doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + day - 1;
  int doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  long days = (long)era * 146097 + doe - 719468;
  return (uint32_t)(days * 86400L);
}

size_t postFileEncode(uint8_t* out, size_t cap, uint32_t last_known_time, const PostRec* recs, uint8_t count) {
  if (cap < POSTFILE_HEADER_SIZE) return 0;
  postEncodeHeader(out, count, last_known_time);
  size_t n = POSTFILE_HEADER_SIZE;
  for (uint8_t i = 0; i < count; i++) {
    size_t tl = recs[i].text_len > POST_MAX_TEXT ? POST_MAX_TEXT : recs[i].text_len;
    if (cap - n < POST_REC_FIXED + tl) return 0;
    n += postEncodeRec(&out[n], recs[i]);
  }
  if (cap - n < count) return 0;
  for (uint8_t i = 0; i < count; i++) out[n++] = recs[i].flags;
  return n;
}

bool postFileDecode(const uint8_t* in, size_t len, uint32_t& last_known_time, PostRec* recs, uint8_t max_recs,
                    uint8_t& count) {
  uint8_t c;
  if (!postDecodeHeader(in, len, c, last_known_time)) return false;
  if (c > max_recs) return false;
  size_t n = POSTFILE_HEADER_SIZE;
  for (uint8_t i = 0; i < c; i++) {
    size_t used = postDecodeRec(&in[n], len - n, recs[i]);
    if (!used) return false;
    n += used;
  }
  for (uint8_t i = 0; i < c; i++) recs[i].flags = (len - n >= c) ? in[n + i] : 0;
  count = c;
  return true;
}

}  // namespace spark

#if defined(ARDUINO) && !defined(SPARK_NATIVE_TEST)

namespace spark {

#define POSTS_FILE "/posts"
#define POSTS_TMP  "/posts.tmp"

static const int MAX_FLAGS = 64;

// Streams records through a small stack buffer; returns count or -1.
static int loadFile(FILESYSTEM* fs, const char* path, PostStore::PutFn put, void* ctx, uint32_t& last_known) {
  File f = fs->open(path, FILE_O_READ);
  if (!f) return -1;
  uint8_t hdr[POSTFILE_HEADER_SIZE];
  uint8_t count = 0;
  int result = -1;
  if (f.read(hdr, sizeof(hdr)) == (int)sizeof(hdr) && postDecodeHeader(hdr, sizeof(hdr), count, last_known)) {
    // First pass validates everything (so a torn file never half-fills the queue),
    // second pass hands the records out.
    PostRec rec;
    uint8_t buf[POST_REC_MAX];
    bool ok = true;
    uint8_t flags[MAX_FLAGS] = { 0 };
    if (count > MAX_FLAGS) ok = false;
    for (int pass = 0; pass < 2 && ok; pass++) {
      f.seek(POSTFILE_HEADER_SIZE);
      for (uint8_t i = 0; i < count && ok; i++) {
        if (f.read(buf, POST_REC_FIXED) != (int)POST_REC_FIXED) { ok = false; break; }
        size_t tl = buf[36];
        if (tl > POST_MAX_TEXT) { ok = false; break; }
        if (tl && f.read(&buf[POST_REC_FIXED], tl) != (int)tl) { ok = false; break; }
        if (!postDecodeRec(buf, POST_REC_FIXED + tl, rec)) { ok = false; break; }
        if (pass == 1) { rec.flags = flags[i]; put(ctx, rec); }
      }
      if (pass == 0 && ok && f.read(flags, count) != (int)count) memset(flags, 0, sizeof(flags));   // old file: no flags
    }
    if (ok) result = count;
  }
  f.close();
  return result;
}

int PostStore::load(PutFn put, void* ctx, uint32_t& last_known_time) {
  if (!_ok) return -1;
  int n = loadFile(_fs, POSTS_FILE, put, ctx, last_known_time);
  if (n < 0) n = loadFile(_fs, POSTS_TMP, put, ctx, last_known_time);
  return n;
}

bool PostStore::save(int count, GetFn get, void* ctx, uint32_t last_known_time) {
  if (!_ok) return false;
  if (count < 0) count = 0;
  if (count > MAX_FLAGS) count = MAX_FLAGS;
  _fs->remove(POSTS_TMP);
  File f = _fs->open(POSTS_TMP, FILE_O_WRITE);
  if (!f) return false;
  uint8_t buf[POST_REC_MAX];
  uint32_t total = POSTFILE_HEADER_SIZE;
  bool ok = true;
  postEncodeHeader(buf, (uint8_t)count, last_known_time);
  ok = f.write(buf, POSTFILE_HEADER_SIZE) == POSTFILE_HEADER_SIZE;
  PostRec rec;
  uint8_t flags[MAX_FLAGS];
  for (int i = 0; i < count && ok; i++) {
    if (!get(ctx, i, rec)) { ok = false; break; }
    size_t n = postEncodeRec(buf, rec);
    ok = f.write(buf, n) == n;
    total += n;
    flags[i] = rec.flags;
  }
  if (ok && count > 0) { ok = f.write(flags, count) == (size_t)count; total += count; }
  f.close();
  if (!ok) { _fs->remove(POSTS_TMP); return false; }
  // /posts.tmp is complete. Replace /posts; if power fails between the remove and the
  // rename, load() falls back to /posts.tmp.
  _fs->remove(POSTS_FILE);
  if (!_fs->rename(POSTS_TMP, POSTS_FILE)) return false;
  _used_kb = (total + 1023) / 1024;
  return true;
}

bool PostStore::loop(unsigned long now_ms, int count, GetFn get, void* ctx, uint32_t last_known_time) {
  if (!_ok || !_dirty) return false;
  if ((long)(now_ms - _dirty_at) < (long)DEBOUNCE_MS) return false;
  _dirty = false;
  save(count, get, ctx, last_known_time);
  return true;
}

}  // namespace spark
#endif
