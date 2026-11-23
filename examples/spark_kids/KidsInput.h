#pragma once

// Touch gesture recognizer for the SpArkKids UI (pure C++, unit tested natively).
// Feed it one touch sample per poll; it emits taps (on release), vertical scrolls with a
// coasting fling, and a left swipe (finger moves right to left) that means "back".
// Thresholds: kids-tdeck-ui-plan.md section 4.3.

#include <stdint.h>

namespace kids {

enum UiType : uint8_t {
  UI_NONE = 0,
  UI_PRESS,       // finger down at (x, y): highlight the target
  UI_CANCEL,      // the press will not become a tap: remove the highlight
  UI_TAP,         // tap at (x, y) = where the finger went down
  UI_SCROLL,      // content follows the finger by dy (finger down = positive)
  UI_BACK_HINT,   // horizontal drag towards "back": progress 0..100 (100 = release goes back)
  UI_BACK,        // left swipe completed
};

struct UiEvent {
  UiType type;
  int16_t x, y;
  int16_t dy;
  uint8_t progress;
};

struct GestureCfg {
  int16_t slop = 10;               // px: movement that is no longer a tap
  uint16_t long_ms = 650;          // a press this long is not a tap
  int16_t swipe_min = 56;          // px
  uint16_t swipe_ms = 800;         // max duration, unless the swipe is long
  int16_t swipe_long = 140;        // px: counts at any speed
  float swipe_angle = 0.58f;       // |dy| <= angle * |dx|
  float axis_ratio = 1.2f;         // |dx| > ratio * |dy| locks horizontal, else vertical
  uint16_t release_silence_ms = 100;
  uint16_t tap_guard_ms = 150;     // ignore a tap this soon after the previous release
  float fling_min = 0.3f;          // px/ms
  float fling_max = 3.0f;
  float fling_stop = 0.05f;
  float fling_tau_ms = 250.0f;
};

class Gesture {
public:
  explicit Gesture(const GestureCfg& cfg = GestureCfg()) : _c(cfg) { reset(); }
  void reset();

  // points: 0 = no finger, 1 = one finger, >1 = several (cancels). Returns events written (<= 2).
  int feed(uint8_t points, int16_t x, int16_t y, uint32_t now, UiEvent* out);
  // Call every loop: release-by-silence and fling steps. Returns events written (<= 1).
  int tick(uint32_t now, UiEvent* out);
  // A new screen was opened: ignore the rest of this touch (one gesture, one effect).
  void swallow();
  bool active() const { return _state != IDLE || _fling != 0.0f; }
  bool down() const { return _state != IDLE; }

private:
  enum State : uint8_t { IDLE, PRESSED, DRAG_V, DRAG_H, DEAD };
  GestureCfg _c;
  State _state;
  int16_t _x0, _y0, _lx, _ly;
  uint32_t _t0, _last_sample, _last_release;
  // velocity: the sample about 60 ms ago
  int16_t _vy_ref; uint32_t _vt_ref;
  float _v;                // px/ms of the last vertical drag samples
  float _fling;            // px/ms while coasting, 0 = none
  float _fling_acc;
  uint32_t _fling_t;
  int release(uint32_t now, UiEvent* out);
};

// Linear touch calibration: raw GT911 coordinates (portrait panel, 240 x 320) to the
// landscape screen (rotation 3: x = raw_y, y = 239 - raw_x), scaled by the raw ranges.
struct TouchCal {
  int16_t x0, x1;   // raw_x range: raw x0 -> screen y 239, raw x1 -> screen y 0
  int16_t y0, y1;   // raw_y range: raw y0 -> screen x 0,   raw y1 -> screen x 319
};
void touchMap(const TouchCal& cal, int16_t raw_x, int16_t raw_y, int16_t& sx, int16_t& sy);
// Solve the calibration from two taps at known screen points (sx_a, sy_a) and (sx_b, sy_b).
bool touchSolve(int16_t sx_a, int16_t sy_a, int16_t rx_a, int16_t ry_a, int16_t sx_b, int16_t sy_b,
                int16_t rx_b, int16_t ry_b, TouchCal& out);

}  // namespace kids
