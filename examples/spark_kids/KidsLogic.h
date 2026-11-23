#pragma once

// Pure helpers of the SpArkKids firmware (no Arduino), unit tested in test/test_spark_members.

#include <stdint.h>
#include <stddef.h>

namespace kids {

// --- text -----------------------------------------------------------------------------

// Folds UTF-8 to the 7-bit ASCII the built-in fonts can draw: Latin-1 letters lose their
// accent (é -> e, ß -> ss), common punctuation is mapped, anything else becomes '?'.
// Emoji and other 4-byte sequences become a single '?'. Returns the output length.
size_t foldUtf8(const char* in, char* out, size_t cap);

// Width of a character in pixels, supplied by the display code.
typedef int (*CharWidthFn)(char c, void* ctx);

// Word wrap: splits `text` into lines no wider than max_w. Calls `line(start, len, ctx)` for
// each line (leading spaces of continuation lines are skipped). Returns number of lines.
typedef void (*LineFn)(const char* start, int len, void* ctx);
int wrapText(const char* text, int max_w, CharWidthFn cw, void* cw_ctx, LineFn line, void* line_ctx);

// --- location policy --------------------------------------------------------------------

static const uint32_t LOC_MOVING_SECS = 5 * 60;   // at most every 5 min while moving
static const uint32_t LOC_STILL_SECS = 15 * 60;   // heartbeat every 15 min while still
static const uint32_t LOC_MOVE_METERS = 100;      // "moving" = more than this since the last send
static const uint32_t LOC_RETRY_SECS = 60;        // no reply from the room: try again after this

// Distance in meters between two points (x1e6 degrees), equirectangular approximation.
uint32_t distanceMeters(int32_t lat1, int32_t lon1, int32_t lat2, int32_t lon2);

struct LocState {
  bool sent;           // a fix was confirmed by the room at least once
  uint32_t sent_at;    // uptime secs of the last confirmed send
  int32_t lat, lon;    // the last confirmed fix
  uint32_t tried_at;   // uptime secs of the last attempt (0 = none pending)
};

// Should a fix (lat, lon) be sent now (uptime secs)?
bool shouldSendLocation(const LocState& s, uint32_t now, int32_t lat, int32_t lon);

// --- clock ------------------------------------------------------------------------------

// The room rejects a login whose timestamp is not newer than the previous one. The device
// has no RTC battery, so it persists a floor and starts from it after a reboot.
static const uint32_t CLOCK_FLOOR_MARGIN = 150;     // saved floor = now + margin
static const uint32_t CLOCK_FLOOR_SAVE_SECS = 120;  // re-save interval (must be < margin)
// The clock value to use at boot from the RTC value and the persisted floor.
uint32_t bootClock(uint32_t rtc_now, uint32_t saved_floor);
// Whether to accept a time offered by the room (login reply): only forward, and only when
// no GPS time was set in the last hour (GPS is better).
bool acceptRoomTime(uint32_t now, uint32_t room_time, bool gps_time_recent);

// Local time from a POSIX TZ string (.env SPARK_TZ), e.g. "CET-1CEST,M3.5.0,M10.5.0/3",
// "EST5EDT,M3.2.0,M11.1.0", "<+0530>-5:30" or "UTC0". DST rules: Mm.w.d, Jn or n, each with an
// optional /time (may be negative or above 24 h). Offsets are POSIX: positive = west of UTC.
struct TzRule {
  int32_t std_off;       // seconds to ADD to UTC for local standard time
  int32_t dst_off;       // the same during DST
  bool has_dst;
  struct When { uint8_t kind; uint16_t m, w, d; int32_t secs; } start, end;   // kind: 0 Mm.w.d, 1 Jn, 2 n
};
// Parses tz; returns false (and r = UTC) when it is not a valid POSIX TZ string.
bool parseTz(const char* tz, TzRule& r);
// Offset in seconds to add to a UTC epoch time to get local time.
int32_t tzOffset(const TzRule& r, uint32_t utc);

// --- Public channel ---------------------------------------------------------------------

// KidsPrefs.public_mode: 0 = follow the build default (.env KIDS_PUBLIC_ENABLED), 1 = off,
// 2 = on. A parent's toggle stores 1 or 2, so it survives reboots and later builds.
static const uint8_t PUBLIC_DEFAULT = 0, PUBLIC_OFF = 1, PUBLIC_ON = 2;
bool publicEnabled(uint8_t mode, bool build_default);
// The value to store when a parent toggles the setting.
uint8_t publicToggled(uint8_t mode, bool build_default);

// Posting limit: at most per_hour posts (1..PUBLIC_RATE_SLOTS) in any 60 minutes, and at
// least PUBLIC_MIN_GAP_SECS between two posts. Times are uptime seconds (non-zero).
static const uint32_t PUBLIC_MIN_GAP_SECS = 30;
static const int PUBLIC_RATE_SLOTS = 16;
struct PublicRate {
  uint32_t sent[PUBLIC_RATE_SLOTS];   // ring of recent post times, 0 = empty
  uint8_t next;
};
// May a post go out at `now`? When not, *wait_secs (if given) is how long until it may.
bool publicRateAllow(const PublicRate& r, uint32_t now, int per_hour, uint32_t* wait_secs);
void publicRateRecord(PublicRate& r, uint32_t now);

// MeshCore channel text is "<name>: <text>". Copies the name (at most cap-1 bytes, empty if
// there is no ": " in the first 32 bytes) and returns a pointer to the text.
const char* splitChannelText(const char* text, char* name, size_t cap);
// A stable 4-byte tag from a sender name (colour and duplicate check of Public posts).
void nameTag(const char* name, uint8_t* tag4);

// --- misc -------------------------------------------------------------------------------

// Battery bars 0..4 from millivolts (Li-ion, under light load).
int batteryBars(uint16_t mv);
// A stable colour index 0..n-1 for an author key prefix.
int authorColor(const uint8_t* prefix, int n);
// Hex of the first n bytes; out must hold 2n+1.
void toHex(char* out, const uint8_t* in, int n);
// Parses 64 hex digits into a 32-byte key. Spaces are ignored.
bool parseKey(const char* hex, uint8_t* key);

}  // namespace kids
