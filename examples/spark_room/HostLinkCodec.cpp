#include "HostLinkCodec.h"
#include "CompanionProtocol.h"
#include <string.h>
#include <stdio.h>
#include <stdarg.h>

namespace hl {

static inline void put16(uint8_t* p, uint16_t v) { p[0] = v & 0xFF; p[1] = v >> 8; }
static inline void put32(uint8_t* p, uint32_t v) {
  p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF; p[2] = (v >> 16) & 0xFF; p[3] = v >> 24;
}
static inline uint32_t get32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
// copy `s` into a NUL padded field of `n` bytes (always zero-fills the rest)
static void putField(uint8_t* p, size_t n, const char* s) {
  memset(p, 0, n);
  if (s) {
    size_t l = strlen(s);
    if (l > n) l = n;
    memcpy(p, s, l);
  }
}

size_t truncUtf8(const char* s, size_t len, size_t max) {
  if (len <= max) return len;
  size_t n = max;
  // if the byte after the cut is a continuation byte, we are inside a character: back up
  while (n > 0 && (((uint8_t)s[n]) & 0xC0) == 0x80) n--;
  return n;
}

size_t buildOk(uint8_t* out) { out[0] = RESP_CODE_OK; return 1; }
size_t buildErr(uint8_t* out, uint8_t code) { out[0] = RESP_CODE_ERR; out[1] = code; return 2; }
size_t buildNoMore(uint8_t* out) { out[0] = RESP_CODE_NO_MORE_MESSAGES; return 1; }
size_t buildMsgWaiting(uint8_t* out) { out[0] = PUSH_CODE_MSG_WAITING; return 1; }
size_t buildCurrTime(uint8_t* out, uint32_t epoch) { out[0] = RESP_CODE_CURR_TIME; put32(&out[1], epoch); return 5; }

size_t buildSent(uint8_t* out) {
  memset(out, 0, 10);
  out[0] = RESP_CODE_SENT;
  return 10;
}

size_t buildBattStorage(uint8_t* out, uint16_t batt_mv, uint32_t used_kb, uint32_t total_kb) {
  out[0] = RESP_CODE_BATT_AND_STORAGE;
  put16(&out[1], batt_mv);
  put32(&out[3], used_kb);
  put32(&out[7], total_kb);
  return 11;
}

size_t buildStatsCore(uint8_t* out, uint16_t batt_mv, uint32_t uptime_secs, uint16_t err_flags, uint8_t tx_queue_len) {
  out[0] = RESP_CODE_STATS; out[1] = STATS_TYPE_CORE;
  put16(&out[2], batt_mv);
  put32(&out[4], uptime_secs);
  put16(&out[8], err_flags);
  out[10] = tx_queue_len;
  return 11;
}

size_t buildStatsRadio(uint8_t* out, int16_t noise_floor, int8_t last_rssi, int8_t last_snr_x4,
                       uint32_t tx_air_secs, uint32_t rx_air_secs) {
  out[0] = RESP_CODE_STATS; out[1] = STATS_TYPE_RADIO;
  put16(&out[2], (uint16_t)noise_floor);
  out[4] = (uint8_t)last_rssi;
  out[5] = (uint8_t)last_snr_x4;
  put32(&out[6], tx_air_secs);
  put32(&out[10], rx_air_secs);
  return 14;
}

size_t buildStatsPackets(uint8_t* out, uint32_t recv, uint32_t sent, uint32_t sent_flood, uint32_t sent_direct,
                         uint32_t recv_flood, uint32_t recv_direct, uint32_t recv_errors) {
  out[0] = RESP_CODE_STATS; out[1] = STATS_TYPE_PACKETS;
  put32(&out[2], recv);
  put32(&out[6], sent);
  put32(&out[10], sent_flood);
  put32(&out[14], sent_direct);
  put32(&out[18], recv_flood);
  put32(&out[22], recv_direct);
  put32(&out[26], recv_errors);
  return 30;
}

size_t buildChannelInfo(uint8_t* out, uint8_t idx, const char* name, const uint8_t secret[16]) {
  out[0] = RESP_CODE_CHANNEL_INFO;
  out[1] = idx;
  putField(&out[2], 32, name);
  memcpy(&out[34], secret, 16);
  return 50;
}

size_t buildCliReply(uint8_t* out, const char* text) {
  out[0] = RESP_CODE_CLI_REPLY;
  size_t l = text ? strlen(text) : 0;
  if (l > MAX_FRAME - 1) l = truncUtf8(text, l, MAX_FRAME - 1);
  if (l) memcpy(&out[1], text, l);
  return 1 + l;
}

size_t buildSelfInfo(uint8_t* out, const SelfInfo& s) {
  size_t i = 0;
  out[i++] = RESP_CODE_SELF_INFO;
  out[i++] = s.adv_type;
  out[i++] = s.tx_power;
  out[i++] = s.max_tx_power;
  memcpy(&out[i], s.pubkey, PUBKEY_SIZE); i += PUBKEY_SIZE;
  put32(&out[i], (uint32_t)s.lat); i += 4;
  put32(&out[i], (uint32_t)s.lon); i += 4;
  out[i++] = s.multi_acks;
  out[i++] = s.advert_loc_policy;
  out[i++] = 0;   // telemetry modes
  out[i++] = 0;   // manual add contacts
  put32(&out[i], s.freq_khz); i += 4;
  put32(&out[i], s.bw_hz); i += 4;
  out[i++] = s.sf;
  out[i++] = s.cr;
  size_t room = MAX_FRAME - i;
  size_t nl = s.name ? strlen(s.name) : 0;
  if (nl > room) nl = truncUtf8(s.name, nl, room);
  if (nl) memcpy(&out[i], s.name, nl);
  i += nl;
  return i;
}

size_t buildDeviceInfo(uint8_t* out, const DeviceInfo& d) {
  size_t i = 0;
  out[i++] = RESP_CODE_DEVICE_INFO;
  out[i++] = HOSTLINK_FIRMWARE_VER_CODE;
  out[i++] = 0;
  out[i++] = 1;   // max channels
  put32(&out[i], 0); i += 4;   // BLE PIN is never disclosed
  putField(&out[i], 12, d.build_date); i += 12;
  putField(&out[i], 40, d.board_model); i += 40;
  putField(&out[i], 20, d.version); i += 20;
  out[i++] = d.repeat_enabled ? 1 : 0;
  out[i++] = d.path_hash_mode;
  return i;
}

size_t buildContact(uint8_t* out, uint8_t code, const Contact& c) {
  size_t i = 0;
  out[i++] = code;
  memcpy(&out[i], c.pubkey, PUBKEY_SIZE); i += PUBKEY_SIZE;
  out[i++] = c.adv_type;
  out[i++] = 0;      // flags
  out[i++] = 0xFF;   // out_path_len: unknown
  memset(&out[i], 0, 64); i += 64;
  memcpy(&out[i], c.name, 32); i += 32;
  put32(&out[i], c.last_advert_ts); i += 4;
  put32(&out[i], (uint32_t)c.lat); i += 4;
  put32(&out[i], (uint32_t)c.lon); i += 4;
  put32(&out[i], c.lastmod); i += 4;
  return i;
}

size_t buildContactsStart(uint8_t* out, uint32_t count) {
  out[0] = RESP_CODE_CONTACTS_START; put32(&out[1], count); return 5;
}
size_t buildEndOfContacts(uint8_t* out, uint32_t lastmod) {
  out[0] = RESP_CODE_END_OF_CONTACTS; put32(&out[1], lastmod); return 5;
}

size_t buildRoomPost(uint8_t* out, const uint8_t room_prefix6[6], uint32_t post_ts,
                     const uint8_t author_prefix4[4], const char* text, size_t text_len) {
  size_t i = 0;
  out[i++] = RESP_CODE_CONTACT_MSG_RECV_V3;
  out[i++] = 0;   // snr
  out[i++] = 0; out[i++] = 0;
  memcpy(&out[i], room_prefix6, 6); i += 6;
  out[i++] = 0xFF;   // path_len: direct/unknown
  out[i++] = 2;      // txt_type: signed plain (room post)
  put32(&out[i], post_ts); i += 4;
  memcpy(&out[i], author_prefix4, 4); i += 4;
  size_t room = MAX_FRAME - i;
  if (text_len > room) text_len = truncUtf8(text, text_len, room);
  if (text_len) memcpy(&out[i], text, text_len);
  return i + text_len;
}

size_t buildChannelMsg(uint8_t* out, int8_t snr_x4, uint8_t chan_idx, uint8_t path_len, uint32_t sender_ts,
                       const char* text, size_t text_len) {
  size_t i = 0;
  out[i++] = RESP_CODE_CHANNEL_MSG_RECV_V3;
  out[i++] = (uint8_t)snr_x4;
  out[i++] = 0; out[i++] = 0;
  out[i++] = chan_idx;
  out[i++] = path_len;
  out[i++] = 0;   // txt_type plain
  put32(&out[i], sender_ts); i += 4;
  size_t room = MAX_FRAME - i;
  if (text_len > room) text_len = truncUtf8(text, text_len, room);
  if (text_len) memcpy(&out[i], text, text_len);
  return i + text_len;
}

bool parseChannelSend(const uint8_t* f, size_t len, ChannelSendCmd& c) {
  if (len < 7 || f[0] != CMD_SEND_CHANNEL_TXT_MSG) return false;
  c.txt_type = f[1];
  c.chan_idx = f[2];
  c.timestamp = get32(&f[3]);
  c.text = (const char*)&f[7];
  c.text_len = len - 7;
  c.sender = nullptr;
  c.sender_len = 0;
  const void* nul = c.text_len ? memchr(c.text, 0, c.text_len) : nullptr;
  if (nul) {
    size_t at = (const char*)nul - c.text;
    const char* s = c.text + at + 1;
    size_t sl = c.text_len - at - 1;
    const void* end = sl ? memchr(s, 0, sl) : nullptr;
    if (end) sl = (const char*)end - s;
    c.text_len = at;
    if (sl) { c.sender = s; c.sender_len = sl; }
  }
  return true;
}

size_t sanitizeSenderName(char* out, const char* in, size_t len) {
  size_t n = 0;
  bool space = false;
  for (size_t i = 0; i < len && in && in[i]; i++) {
    uint8_t ch = (uint8_t)in[i];
    if (ch == ':') continue;
    if (ch < 0x20 || ch == 0x7F || ch == ' ') { space = true; continue; }
    if (space && n > 0 && n < MAX_SENDER_NAME) out[n++] = ' ';
    space = false;
    if (n >= MAX_SENDER_NAME) break;
    out[n++] = (char)ch;
  }
  // a cut may end inside a multi-byte character: drop the partial sequence
  size_t k = n;
  while (k > 0 && (((uint8_t)out[k - 1]) & 0xC0) == 0x80) k--;   // continuation bytes
  if (k > 0 && ((uint8_t)out[k - 1]) >= 0xC0) {
    uint8_t lead = (uint8_t)out[k - 1];
    size_t need = lead >= 0xF0 ? 4 : lead >= 0xE0 ? 3 : 2;
    if (n - (k - 1) < need) n = k - 1;
  }
  while (n > 0 && out[n - 1] == ' ') n--;
  out[n] = 0;
  return n;
}

bool parseTxtSend(const uint8_t* f, size_t len, TxtSendCmd& c) {
  if (len < 13 || f[0] != CMD_SEND_TXT_MSG) return false;
  c.txt_type = f[1];
  c.attempt = f[2];
  c.timestamp = get32(&f[3]);
  c.dest6 = &f[7];
  c.text = (const char*)&f[13];
  c.text_len = len - 13;
  return true;
}

size_t composeChannelText(char* out, const char* name, const char* text, size_t text_len) {
  size_t nl = name ? strlen(name) : 0;
  size_t n = 0;
  if (nl > MAX_CHAN_TEXT - 2) nl = truncUtf8(name, nl, MAX_CHAN_TEXT - 2);
  memcpy(out, name, nl); n = nl;
  out[n++] = ':'; out[n++] = ' ';
  // stop at an embedded NUL: the on-air text is a C string
  size_t tl = 0;
  while (tl < text_len && text[tl]) tl++;
  size_t room = MAX_CHAN_TEXT - n;
  if (tl > room) tl = truncUtf8(text, tl, room);
  memcpy(&out[n], text, tl); n += tl;
  out[n] = 0;
  return n;
}

UsbFrameParser::Result UsbFrameParser::feed(uint8_t b, uint32_t now_ms) {
  last_ms = now_ms;
  switch (state) {
    case IDLE:
      if (b != '<') return NONE;
      state = LEN_LO; got = 0;
      return NONE;
    case LEN_LO:
      need = b; state = LEN_HI;
      return NONE;
    case LEN_HI:
      need |= ((size_t)b) << 8;
      if (need == 0 || need > MAX_FRAME) { reset(); return ABORT; }
      got = 0; state = PAYLOAD;
      return NONE;
    case PAYLOAD:
      buf[got++] = b;
      if (got == need) { state = IDLE; return FRAME; }
      return NONE;
  }
  return NONE;
}

bool UsbFrameParser::tick(uint32_t now_ms) {
  if (state != IDLE && (uint32_t)(now_ms - last_ms) >= TIMEOUT_MS) { reset(); return true; }
  return false;
}

size_t wrapWire(uint8_t* out, const uint8_t* payload, size_t len) {
  out[0] = '>';
  out[1] = len & 0xFF;
  out[2] = (len >> 8) & 0xFF;
  memmove(&out[3], payload, len);
  return len + 3;
}

// ---- TRACE ----

static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool parseTraceHops(const char* s, uint8_t* out, size_t& n_bytes, uint8_t& flags) {
  n_bytes = 0;
  flags = 0;
  size_t hop_sz = 0, hops = 0;
  const char* p = s;
  for (;;) {
    while (*p == ' ') p++;
    size_t digits = 0;
    const char* q = p;
    while (hexNibble(*q) >= 0) { q++; digits++; }
    const char* e = q;
    while (*e == ' ') e++;
    if (digits == 0 || (*e != ',' && *e != 0) || (digits & 1)) return false;
    size_t sz = digits / 2;
    if (sz != 1 && sz != 2 && sz != 4) return false;
    if (hop_sz == 0) hop_sz = sz;
    else if (sz != hop_sz) return false;
    if (++hops > MAX_TRACE_HOPS) return false;
    for (size_t i = 0; i < sz; i++) out[n_bytes++] = (uint8_t)((hexNibble(p[2 * i]) << 4) | hexNibble(p[2 * i + 1]));
    if (*e == 0) break;
    p = e + 1;   // past the comma; a trailing comma fails on the empty hop
  }
  flags = hop_sz == 1 ? 0 : hop_sz == 2 ? 1 : 2;
  return true;
}

bool parseTraceSend(const uint8_t* f, size_t len, TraceSendCmd& c, size_t max_hops) {
  if (len <= 10 || len - 10 >= 184 - 5) return false;
  uint8_t path_len = (uint8_t)(len - 10);
  uint8_t flags = f[9];
  uint8_t sz = flags & 0x03;
  if ((size_t)(path_len >> sz) > max_hops || (path_len % (1 << sz)) != 0) return false;
  c.tag = get32(&f[1]);
  c.auth = get32(&f[5]);
  c.flags = flags;
  c.path = &f[10];
  c.path_len = path_len;
  return true;
}

size_t buildTraceSent(uint8_t* out, uint32_t tag, uint32_t est_timeout_ms) {
  out[0] = RESP_CODE_SENT;
  out[1] = 0;
  put32(&out[2], tag);
  put32(&out[6], est_timeout_ms);
  return 10;
}

size_t buildTraceData(uint8_t* out, uint32_t tag, uint32_t auth, uint8_t flags, const uint8_t* hashes,
                      const int8_t* snrs, uint8_t path_len, int8_t final_snr_x4) {
  uint8_t sz = flags & 0x03;
  size_t n_snr = path_len >> sz;
  if (12 + path_len + n_snr + 1 > MAX_FRAME) return 0;
  size_t i = 0;
  out[i++] = PUSH_CODE_TRACE_DATA;
  out[i++] = 0;   // reserved
  out[i++] = path_len;
  out[i++] = flags;
  put32(&out[i], tag); i += 4;
  put32(&out[i], auth); i += 4;
  memcpy(&out[i], hashes, path_len); i += path_len;
  memcpy(&out[i], snrs, n_snr); i += n_snr;
  out[i++] = (uint8_t)final_snr_x4;
  return i;
}

size_t formatSnr(char* out, size_t cap, int8_t snr_x4) {
  int v = snr_x4;
  bool neg = v < 0;
  if (neg) v = -v;
  int tenths = (v * 10 + 2) / 4;
  int n = snprintf(out, cap, "%s%d.%d", (neg && tenths) ? "-" : "", tenths / 10, tenths % 10);
  return n < 0 ? 0 : (size_t)n;
}

static size_t appendf(char* out, size_t cap, size_t at, const char* fmt, ...) {
  if (at >= cap - 1) return at;
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(out + at, cap - at, fmt, ap);
  va_end(ap);
  if (n < 0) return at;
  return (size_t)n >= cap - at ? cap - 1 : at + n;
}

size_t formatTraceResult(char* out, size_t cap, const TraceResult& r, uint32_t age_s) {
  if (cap == 0) return 0;
  out[0] = 0;
  size_t at = 0;
  char snr[12];
  switch (r.state) {
    case TraceResult::PENDING:
      return appendf(out, cap, 0, "> pending tag=%08X age=%us", (unsigned)r.tag, (unsigned)age_s);
    case TraceResult::TIMEOUT:
      return appendf(out, cap, 0, "> timeout (no reply after %u s) tag=%08X", (unsigned)(TRACE_TIMEOUT_MS / 1000),
                     (unsigned)r.tag);
    case TraceResult::DONE: break;
    default:
      return appendf(out, cap, 0, "> none");
  }
  size_t sz = (size_t)1 << (r.flags & 3);
  (void)age_s;   // kept short: the whole result must fit the 150 byte CLI reply
  at = appendf(out, cap, at, "> tag=%08X", (unsigned)r.tag);
  for (size_t h = 0; h < r.n_hops; h++) {
    at = appendf(out, cap, at, "\n");
    for (size_t b = 0; b < sz; b++) at = appendf(out, cap, at, "%02x", r.hashes[h * sz + b]);
    formatSnr(snr, sizeof(snr), r.snr_x4[h]);
    at = appendf(out, cap, at, " %sdB", snr);
  }
  formatSnr(snr, sizeof(snr), r.final_snr_x4);
  at = appendf(out, cap, at, "\nback %sdB rssi=%d", snr, (int)r.final_rssi);
  return at;
}

size_t formatNames(char* out, size_t cap, const Contact* names, const bool* used, int count, int start) {
  if (cap == 0) return 0;
  out[0] = 0;
  int order[64];
  int n = 0;
  if (count > 64) count = 64;
  for (int i = 0; i < count; i++) {
    if (!used[i]) continue;
    int j = n++;
    while (j > 0 && names[order[j - 1]].lastmod < names[i].lastmod) { order[j] = order[j - 1]; j--; }
    order[j] = i;
  }
  if (n == 0) return appendf(out, cap, 0, "> no names heard yet");
  if (start < 0 || start >= n) return appendf(out, cap, 0, "> only %d names", n);
  size_t at = 0;
  for (int k = start; k < n; k++) {
    const Contact& c = names[order[k]];
    char line[48];
    int l = snprintf(line, sizeof(line), "%s%02x%02x%02x%02x %.31s", k == start ? "" : "\n", c.pubkey[0], c.pubkey[1],
                     c.pubkey[2], c.pubkey[3], c.name);
    if (l < 0) break;
    // keep room for the "more" hint
    if (at + (size_t)l + 22 >= cap && k + 1 < n) {
      at = appendf(out, cap, at, "\nmore: get names %d", k);
      return at;
    }
    if (at + (size_t)l >= cap) {
      at = appendf(out, cap, at, "\nmore: get names %d", k);
      return at;
    }
    at = appendf(out, cap, at, "%s", line);
  }
  return at;
}

}  // namespace hl
