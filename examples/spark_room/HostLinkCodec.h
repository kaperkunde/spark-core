#pragma once

// Pure HostLink frame builders and parsers. No Arduino or Mesh includes, so this
// compiles in the native test env. All integers little-endian.

#include <stdint.h>
#include <stddef.h>

namespace hl {

static const size_t MAX_FRAME       = 176;
static const size_t MAX_CHAN_TEXT   = 160;   // whole "<name>: <text>" string
static const size_t MAX_POST_TEXT   = 151;
static const size_t MAX_CHAN_FRAME_TEXT = 165;  // 176 - 11 header bytes
static const size_t MAX_SENDER_NAME = 31;  // node_name[32] / contact name[32] incl. NUL
static const size_t PUBKEY_SIZE     = 32;
static const size_t CONTACT_FRAME_SIZE     = 148;
static const size_t DEVICE_INFO_FRAME_SIZE = 82;

// Cut `len` bytes of UTF-8 text down to at most `max` bytes without splitting a
// multi-byte character. Returns the new length.
size_t truncUtf8(const char* s, size_t len, size_t max);

// ---- builders: write into `out` (capacity >= MAX_FRAME), return frame length ----
size_t buildOk(uint8_t* out);
size_t buildErr(uint8_t* out, uint8_t code);
size_t buildNoMore(uint8_t* out);
size_t buildMsgWaiting(uint8_t* out);
size_t buildCurrTime(uint8_t* out, uint32_t epoch);
size_t buildSent(uint8_t* out);
size_t buildBattStorage(uint8_t* out, uint16_t batt_mv, uint32_t used_kb, uint32_t total_kb);
size_t buildStatsCore(uint8_t* out, uint16_t batt_mv, uint32_t uptime_secs, uint16_t err_flags, uint8_t tx_queue_len);
size_t buildStatsRadio(uint8_t* out, int16_t noise_floor, int8_t last_rssi, int8_t last_snr_x4,
                       uint32_t tx_air_secs, uint32_t rx_air_secs);
size_t buildStatsPackets(uint8_t* out, uint32_t recv, uint32_t sent, uint32_t sent_flood, uint32_t sent_direct,
                         uint32_t recv_flood, uint32_t recv_direct, uint32_t recv_errors);
size_t buildChannelInfo(uint8_t* out, uint8_t idx, const char* name, const uint8_t secret[16]);
size_t buildCliReply(uint8_t* out, const char* text);   // cut to fit one frame

struct SelfInfo {
  uint8_t adv_type, tx_power, max_tx_power;
  uint8_t pubkey[PUBKEY_SIZE];
  int32_t lat, lon;
  uint8_t multi_acks, advert_loc_policy;
  uint32_t freq_khz;     // MHz x 1000
  uint32_t bw_hz;        // kHz x 1000
  uint8_t sf, cr;
  const char* name;
};
size_t buildSelfInfo(uint8_t* out, const SelfInfo& s);

struct DeviceInfo {
  const char* build_date;   // cut to 12 bytes
  const char* board_model;  // cut to 40
  const char* version;      // cut to 20
  uint8_t repeat_enabled, path_hash_mode;
};
size_t buildDeviceInfo(uint8_t* out, const DeviceInfo& d);   // always 82 bytes

struct Contact {
  uint8_t pubkey[PUBKEY_SIZE];
  uint8_t adv_type;
  char name[32];            // NUL padded
  uint32_t last_advert_ts;
  int32_t lat, lon;
  uint32_t lastmod;
};
// code = RESP_CODE_CONTACT (3) or PUSH_CODE_NEW_ADVERT (0x8A); always 148 bytes
size_t buildContact(uint8_t* out, uint8_t code, const Contact& c);
size_t buildContactsStart(uint8_t* out, uint32_t count);
size_t buildEndOfContacts(uint8_t* out, uint32_t most_recent_lastmod);

// Room post (CONTACT_MSG_RECV_V3, txt_type 2). room_prefix6/author_prefix4 are pubkey prefixes.
size_t buildRoomPost(uint8_t* out, const uint8_t room_prefix6[6], uint32_t post_ts,
                     const uint8_t author_prefix4[4], const char* text, size_t text_len);
// Channel message (CHANNEL_MSG_RECV_V3).
size_t buildChannelMsg(uint8_t* out, int8_t snr_x4, uint8_t chan_idx, uint8_t path_len, uint32_t sender_ts,
                       const char* text, size_t text_len);

// ---- SpArkNode1.3 extensions ----
// Number of hop-hash bytes a packed path_len byte stands for (0xFF = direct = 0 bytes).
size_t pathByteCount(uint8_t path_len);
// PUSH_CODE_LOG_RX_DATA, upstream companion layout: 0x88, i8 snr x4, i8 rssi, raw packet bytes.
// 0 if the packet does not fit one frame (raw_len > MAX_FRAME - 3): the caller drops it.
size_t buildRxLog(uint8_t* out, int8_t snr_x4, int8_t rssi, const uint8_t* raw, size_t raw_len);
// RESP_CODE_SPARK_CHANNEL_MSG: 0xC1, i8 snr x4, i8 rssi, u8 chan idx, u8 path_len (packed, as V3),
// u8 txt_type (0), u32 sender ts, u8 n (path bytes that follow), path[n], text. The text is never
// cut for the path: if both do not fit, only whole leading hops are kept (n < pathByteCount).
size_t buildChannelMsgPath(uint8_t* out, int8_t snr_x4, int8_t rssi, uint8_t chan_idx, uint8_t path_len,
                           uint32_t sender_ts, const uint8_t* path, const char* text, size_t text_len);
// CMD_SPARK_HOST_FEATURES: 0xC0, u32 requested feature bits. False if malformed.
bool parseHostFeatures(const uint8_t* f, size_t len, uint32_t& bits);
// OK carrying a u32 value (00 + u32), as upstream; used for the enabled feature bits.
size_t buildOkValue(uint8_t* out, uint32_t value);
// Signing (upstream CMD_SIGN_*). The node only signs a JWT signing input: two non-empty
// base64url segments joined by one '.', nothing else (so it can never be made to sign a mesh
// packet or an advert). MAX_SIGN_DATA bytes at most.
static const size_t MAX_SIGN_DATA = 512;
bool isJwtSigningInput(const uint8_t* data, size_t len);
size_t buildSignStart(uint8_t* out, uint32_t max_len);

// ---- command parsers (payload includes the code byte) ----
// SEND_CHANNEL_TXT_MSG: code, txt_type, chan_idx, u32 timestamp, text[, 0x00, sender name].
// The optional trailer (SpArkNode1.2) is everything after the first NUL in the text field, up to
// a second NUL or the frame end. sender_len is 0 when there is no trailer (or it is empty).
struct ChannelSendCmd {
  uint8_t txt_type, chan_idx; uint32_t timestamp;
  const char* text; size_t text_len;
  const char* sender; size_t sender_len;
};
bool parseChannelSend(const uint8_t* f, size_t len, ChannelSendCmd& c);

// Make a host-supplied sender name safe as the "<name>: " head of a channel text: ':' is
// dropped, control characters (newlines, tabs) become spaces, runs of spaces collapse, the
// ends are trimmed, and the result is cut to MAX_SENDER_NAME bytes on a UTF-8 boundary.
// Writes a NUL-terminated string (`out` needs MAX_SENDER_NAME+1 bytes); returns its length,
// 0 if nothing is left (the caller then falls back to the node name).
size_t sanitizeSenderName(char* out, const char* in, size_t len);
struct TxtSendCmd { uint8_t txt_type, attempt; uint32_t timestamp; const uint8_t* dest6; const char* text; size_t text_len; };
bool parseTxtSend(const uint8_t* f, size_t len, TxtSendCmd& c);

// Compose the on-air channel text "<name>: <text>" cut to MAX_CHAN_TEXT on a UTF-8
// boundary. Returns the length written (no NUL counted; a NUL is appended, so `out`
// needs MAX_CHAN_TEXT+1 bytes).
size_t composeChannelText(char* out, const char* name, const char* text, size_t text_len);

// ---- TRACE (path ping) ----
static const size_t MAX_TRACE_HOPS  = 6;      // CLI limit: the result must fit the 150 byte CLI reply
static const size_t MAX_TRACE_BYTES = 24;     // 6 hops of 4 bytes
static const uint32_t TRACE_TIMEOUT_MS = 30000;

// Parse "2d,5f" / "2dbb,5f01" / "2dbb3446": every hop is 1, 2 or 4 bytes of hex, all the same
// size. Fills `out` (raw path bytes), `n_bytes`, and `flags` (lower 2 bits = log2(hop size)).
bool parseTraceHops(const char* s, uint8_t* out, size_t& n_bytes, uint8_t& flags);

// CMD_SEND_TRACE_PATH: cmd(1) tag(4) auth(4) flags(1) path[]. Same validation as upstream
// (len > 10, hop size multiple); path_len is in bytes. max_hops is MAX_PATH_SIZE (64).
struct TraceSendCmd { uint32_t tag, auth; uint8_t flags; const uint8_t* path; uint8_t path_len; };
bool parseTraceSend(const uint8_t* f, size_t len, TraceSendCmd& c, size_t max_hops);
// RESP_CODE_SENT for a trace: 6, 0, tag(4), est_timeout_ms(4)
size_t buildTraceSent(uint8_t* out, uint32_t tag, uint32_t est_timeout_ms);
// PUSH_CODE_TRACE_DATA 0x89, 0, path_len, flags, tag(4), auth(4), hashes[path_len],
// snrs[path_len >> path_sz], final_snr. 0 if it does not fit a frame.
size_t buildTraceData(uint8_t* out, uint32_t tag, uint32_t auth, uint8_t flags, const uint8_t* hashes,
                      const int8_t* snrs, uint8_t path_len, int8_t final_snr_x4);

struct TraceResult {
  enum State { NONE, PENDING, DONE, TIMEOUT };
  uint8_t state = NONE;
  uint32_t tag = 0;
  uint8_t flags = 0;
  uint8_t n_hops = 0;
  uint8_t hashes[MAX_TRACE_BYTES];
  int8_t snr_x4[MAX_TRACE_HOPS];
  int8_t final_snr_x4 = 0;
  int16_t final_rssi = 0;
};
// One line for the CLI / USB serial. age_s = seconds since the trace was sent.
size_t formatTraceResult(char* out, size_t cap, const TraceResult& r, uint32_t age_s);
// "7.3" style (one decimal, rounded half away from zero) from a value in quarter dB.
size_t formatSnr(char* out, size_t cap, int8_t snr_x4);

// `get names [start]`: advert-name cache, newest first, as many entries as fit in `cap`.
// Each entry "<8 hex pubkey prefix> <name>"; ends with "more: get names N" if cut.
size_t formatNames(char* out, size_t cap, const Contact* names, const bool* used, int count, int start);

// ---- USB/TCP wire framing ----
// Host->node frames are '<' len_lo len_hi payload. Byte-at-a-time parser.
class UsbFrameParser {
public:
  enum Result { NONE, FRAME, ABORT };
  static const uint32_t TIMEOUT_MS = 500;

  UsbFrameParser() { reset(); }
  void reset() { state = IDLE; need = got = 0; }
  bool inFrame() const { return state != IDLE; }
  // Feed one byte. In IDLE only '<' is accepted (anything else returns NONE and is
  // ignored, the caller decides whether to route it elsewhere). FRAME: payload() and
  // length() are valid until the next feed(). ABORT: bad length, state reset.
  Result feed(uint8_t b, uint32_t now_ms);
  // Call regularly; drops a half-received frame after TIMEOUT_MS without a byte.
  // Returns true if it dropped one.
  bool tick(uint32_t now_ms);
  const uint8_t* payload() const { return buf; }
  size_t length() const { return got; }
private:
  enum State { IDLE, LEN_LO, LEN_HI, PAYLOAD };
  State state;
  size_t need, got;
  uint32_t last_ms;
  uint8_t buf[MAX_FRAME];
};

// Wrap `payload` in the node->host wire header '>' len_lo len_hi. `out` needs len+3.
size_t wrapWire(uint8_t* out, const uint8_t* payload, size_t len);

}  // namespace hl
