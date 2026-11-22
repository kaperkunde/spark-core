#pragma once

// Room post persistence.
//
// Part 1 (pure, native-testable): the /posts file record codec.
// Part 2 (ARDUINO only): PostStore, the debounced writer/reader on a LittleFS volume.
//
// File format (little-endian):
//   "SPP1", u8 version=1, u8 count, u16 reserved, u32 last_known_time
//   then `count` records, oldest first:
//   u32 post_timestamp, 32-byte author pubkey, u8 text_len, text[text_len]
//   then (optional, absent in files written before the flag existed) `count` flag bytes,
//   one per record: bit 0 = post was created through HostLink (never echoed to the host)

#include <stdint.h>
#include <stddef.h>

namespace spark {

static const size_t POSTFILE_HEADER_SIZE = 12;
static const size_t POST_MAX_TEXT = 151;
static const size_t POST_REC_FIXED = 4 + 32 + 1;
static const size_t POST_REC_MAX = POST_REC_FIXED + POST_MAX_TEXT;
static const uint8_t POSTFILE_VERSION = 1;

static const uint8_t POST_FLAG_HOST = 1;

struct PostRec {
  uint32_t timestamp;
  uint8_t flags;
  uint8_t author[32];
  uint8_t text_len;
  char text[POST_MAX_TEXT + 1];   // NUL terminated copy
};

// header: writes POSTFILE_HEADER_SIZE bytes
void postEncodeHeader(uint8_t* out, uint8_t count, uint32_t last_known_time);
// returns false on bad magic/version
bool postDecodeHeader(const uint8_t* in, size_t len, uint8_t& count, uint32_t& last_known_time);
// record: writes up to POST_REC_MAX bytes, returns the size written
size_t postEncodeRec(uint8_t* out, const PostRec& r);
// returns bytes consumed, or 0 if `len` is too short / text_len is bad
size_t postDecodeRec(const uint8_t* in, size_t len, PostRec& r);

// Epoch seconds (UTC midnight) of a C `__DATE__` string ("Nov 20 2026", day may be space
// padded). Returns 0 if it cannot be parsed. Used as a lower bound for the node clock.
uint32_t epochFromCDate(const char* mmm_dd_yyyy);

// Whole-buffer helpers (tests, small files). Encode returns bytes written or 0 if cap too small.
size_t postFileEncode(uint8_t* out, size_t cap, uint32_t last_known_time, const PostRec* recs, uint8_t count);
// Decodes up to max_recs records; returns false if the header or any record is corrupt.
bool postFileDecode(const uint8_t* in, size_t len, uint32_t& last_known_time, PostRec* recs, uint8_t max_recs,
                    uint8_t& count);

}  // namespace spark

#if defined(ARDUINO) && !defined(SPARK_NATIVE_TEST)
#include <helpers/IdentityStore.h>   // FILESYSTEM

namespace spark {

// Debounced persistence of the room post queue on a LittleFS volume.
class PostStore {
public:
  typedef bool (*GetFn)(void* ctx, int i, PostRec& out);   // i = 0..count-1, oldest first
  typedef void (*PutFn)(void* ctx, const PostRec& rec);

  PostStore() : _fs(0), _dirty_at(0), _dirty(false), _ok(false) { }

  // `fs` must already be mounted. Passing NULL leaves the store off (RAM only).
  void begin(FILESYSTEM* fs) { _fs = fs; _ok = (fs != 0); }
  bool isOn() const { return _ok; }

  // Load /posts (else /posts.tmp). Calls `put` per record, oldest first.
  // Returns record count, or -1 if nothing readable.
  int load(PutFn put, void* ctx, uint32_t& last_known_time);

  void markDirty(unsigned long now_ms) { _dirty = true; _dirty_at = now_ms; }
  // Flush DEBOUNCE_MS after the last markDirty. `count`/`get` describe the queue.
  // Returns true when a write happened.
  bool loop(unsigned long now_ms, int count, GetFn get, void* ctx, uint32_t last_known_time);

  bool save(int count, GetFn get, void* ctx, uint32_t last_known_time);
  void used(uint32_t& used_kb, uint32_t& total_kb) const { used_kb = _used_kb; total_kb = TOTAL_KB; }

  static const unsigned long DEBOUNCE_MS = 3000;
  static const uint32_t TOTAL_KB = 100;
private:
  FILESYSTEM* _fs;
  unsigned long _dirty_at;
  bool _dirty;
  bool _ok;
  uint32_t _used_kb = 0;
};

}  // namespace spark
#endif
