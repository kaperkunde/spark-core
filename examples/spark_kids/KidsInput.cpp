#include "KidsInput.h"

#include <math.h>
#include <stdlib.h>

namespace kids {

void Gesture::reset() {
  _state = IDLE;
  _x0 = _y0 = _lx = _ly = 0;
  _t0 = _last_sample = 0;
  _last_release = 0;
  _vy_ref = 0; _vt_ref = 0;
  _v = 0;
  _fling = 0; _fling_acc = 0; _fling_t = 0;
}

void Gesture::swallow() {
  if (_state != IDLE) _state = DEAD;
  _fling = 0;
}

static UiEvent ev(UiType t, int16_t x = 0, int16_t y = 0, int16_t dy = 0, uint8_t p = 0) {
  UiEvent e;
  e.type = t; e.x = x; e.y = y; e.dy = dy; e.progress = p;
  return e;
}

int Gesture::release(uint32_t now, UiEvent* out) {
  int n = 0;
  State s = _state;
  _state = IDLE;
  uint32_t prev_release = _last_release;
  _last_release = now ? now : 1;
  switch (s) {
    case PRESSED:
      if (now - _t0 < _c.long_ms && (prev_release == 0 || now - prev_release >= _c.tap_guard_ms)) {
        out[n++] = ev(UI_TAP, _x0, _y0);
      } else {
        out[n++] = ev(UI_CANCEL);
      }
      break;
    case DRAG_V:
      if (fabsf(_v) >= _c.fling_min) {
        _fling = _v > _c.fling_max ? _c.fling_max : (_v < -_c.fling_max ? -_c.fling_max : _v);
        _fling_acc = 0;
        _fling_t = now;
      }
      break;
    case DRAG_H: {
      int dx = _lx - _x0, dy = _ly - _y0;
      bool far = dx <= -_c.swipe_min;
      bool timely = (now - _t0) <= _c.swipe_ms || dx <= -_c.swipe_long;
      bool flat = fabsf((float)dy) <= _c.swipe_angle * fabsf((float)dx);
      out[n++] = (far && timely && flat) ? ev(UI_BACK) : ev(UI_BACK_HINT, 0, 0, 0, 0);
      break;
    }
    default:
      break;
  }
  return n;
}

int Gesture::feed(uint8_t points, int16_t x, int16_t y, uint32_t now, UiEvent* out) {
  int n = 0;
  if (points == 0) {
    if (_state != IDLE) n = release(now, out);
    return n;
  }
  _last_sample = now;
  if (points > 1) {   // palm / two fingers: no action until all fingers are up
    if (_state == PRESSED) out[n++] = ev(UI_CANCEL);
    if (_state == DRAG_H) out[n++] = ev(UI_BACK_HINT, 0, 0, 0, 0);
    if (_state != IDLE) _state = DEAD;
    else _state = DEAD;
    return n;
  }
  switch (_state) {
    case IDLE:
      _fling = 0;
      _state = PRESSED;
      _x0 = _lx = x; _y0 = _ly = y;
      _t0 = now;
      _vy_ref = y; _vt_ref = now; _v = 0;
      out[n++] = ev(UI_PRESS, x, y);
      break;
    case PRESSED: {
      int dx = x - _x0, dy = y - _y0;
      if (abs(dx) > _c.slop || abs(dy) > _c.slop) {
        out[n++] = ev(UI_CANCEL);
        if (fabsf((float)dx) > _c.axis_ratio * fabsf((float)dy)) {
          _state = DRAG_H;
        } else {
          _state = DRAG_V;
          out[n++] = ev(UI_SCROLL, x, y, (int16_t)(y - _ly));
        }
      }
      break;
    }
    case DRAG_V:
      if (y != _ly) out[n++] = ev(UI_SCROLL, x, y, (int16_t)(y - _ly));
      break;
    default:
      break;
  }
  if (_state == DRAG_H) {
    int dx = x - _x0;
    int p = dx < 0 ? (-dx * 100) / _c.swipe_min : 0;
    if (p > 100) p = 100;
    if (n < 2) out[n++] = ev(UI_BACK_HINT, x, y, 0, (uint8_t)p);
  }
  if (_state == DRAG_V || _state == PRESSED) {
    if (now - _vt_ref >= 60) {
      _v = (float)(y - _vy_ref) / (float)(now - _vt_ref);
      _vy_ref = y; _vt_ref = now;
    } else if (now > _vt_ref) {
      // keep a running estimate so a quick flick still has a velocity
      float v = (float)(y - _vy_ref) / (float)(now - _vt_ref);
      if (fabsf(v) > fabsf(_v)) _v = v;
    }
  }
  _lx = x; _ly = y;
  return n;
}

int Gesture::tick(uint32_t now, UiEvent* out) {
  if (_state != IDLE && now - _last_sample >= _c.release_silence_ms) {
    // no new frame for a while: the finger was lifted between polls
    return release(now, out);
  }
  if (_fling != 0.0f && _state == IDLE) {
    uint32_t dt = now - _fling_t;
    if (dt == 0) return 0;
    _fling_t = now;
    _fling_acc += _fling * dt;
    _fling *= expf(-(float)dt / _c.fling_tau_ms);
    if (fabsf(_fling) < _c.fling_stop) _fling = 0;
    int16_t step = (int16_t)_fling_acc;
    if (step != 0) {
      _fling_acc -= step;
      out[0] = ev(UI_SCROLL, 0, 0, step);
      return 1;
    }
  }
  return 0;
}

void touchMap(const TouchCal& c, int16_t raw_x, int16_t raw_y, int16_t& sx, int16_t& sy) {
  int dy = c.y1 - c.y0, dx = c.x1 - c.x0;
  if (dy == 0) dy = 1;
  if (dx == 0) dx = 1;
  long x = (long)(raw_y - c.y0) * 319 / dy;
  long y = (long)(c.x1 - raw_x) * 239 / dx;
  sx = (int16_t)(x < 0 ? 0 : (x > 319 ? 319 : x));
  sy = (int16_t)(y < 0 ? 0 : (y > 239 ? 239 : y));
}

bool touchSolve(int16_t sx_a, int16_t sy_a, int16_t rx_a, int16_t ry_a, int16_t sx_b, int16_t sy_b,
                int16_t rx_b, int16_t ry_b, TouchCal& out) {
  if (ry_b == ry_a || rx_b == rx_a || sx_b == sx_a || sy_b == sy_a) return false;
  float k = (float)(sx_b - sx_a) / (float)(ry_b - ry_a);   // screen x per raw y
  float m = (float)(sy_a - sy_b) / (float)(rx_b - rx_a);   // screen y per raw x (inverted)
  // |scale| near 1 (a mirrored axis is fine: touchMap handles y1 < y0); else the taps were bad
  if (fabsf(k) <= 0.5f || fabsf(k) >= 2.0f || fabsf(m) <= 0.5f || fabsf(m) >= 2.0f) return false;
  float y0 = ry_a - sx_a / k;
  float x1 = rx_a + sy_a / m;
  out.y0 = (int16_t)lroundf(y0);
  out.y1 = (int16_t)lroundf(y0 + 319.0f / k);
  out.x1 = (int16_t)lroundf(x1);
  out.x0 = (int16_t)lroundf(x1 - 239.0f / m);
  return true;
}

}  // namespace kids
