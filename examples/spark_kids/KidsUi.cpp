// SpArkKids UI: touch first (GT911, polled), keyboard and trackball as fallbacks.
// Screens: Room (home), People (room, optional Public, members), DM, Public, parent PIN/settings.

#include "KidsApp.h"
#include "Hw.h"
#include <Fonts/FreeSans12pt7b.h>
#include <Fonts/FreeSans9pt7b.h>
#include <Fonts/FreeSansBold9pt7b.h>
#include <math.h>

#define BAR_H 40
#define COMPOSE_H 44
#define BODY_TOP BAR_H
#define BODY_BOTTOM (SCREEN_H - COMPOSE_H)
#define LINE_H 24
#define HDR_H 20
#define ROW_H 52
#define SROW_H 44
#define HIT_TOL 8
#define BACKSPACE_BACK_MS 400

#ifndef TOUCH_CAL_X0
  #define TOUCH_CAL_X0 0
#endif
#ifndef TOUCH_CAL_X1
  #define TOUCH_CAL_X1 224
#endif
#ifndef TOUCH_CAL_Y0
  #define TOUCH_CAL_Y0 2
#endif
#ifndef TOUCH_CAL_Y1
  #define TOUCH_CAL_Y1 314
#endif

static const uint16_t AUTHOR_COLORS[] = { 0x07FF, 0xFFE0, 0xF81F, 0x07E0, 0xFD20, 0x8C7F, 0xFBB7, 0x5FE0 };
static const int CAL_PTS[2][2] = { { 24, 24 }, { 296, 216 } };

static int glyphW(char c, void* f) {
  const GFXfont* font = (const GFXfont*)f;
  if ((uint8_t)c < font->first || (uint8_t)c > font->last) c = '?';
  return font->glyph[(uint8_t)c - font->first].xAdvance;
}
static int textW(const char* s, const GFXfont* f) {
  int w = 0;
  while (*s) w += glyphW(*s++, (void*)f);
  return w;
}
static void fitText(char* s, const GFXfont* f, int maxw) {
  int l = strlen(s);
  if (textW(s, f) <= maxw) return;
  while (l > 0 && textW(s, f) + textW("..", f) > maxw) s[--l] = 0;
  strcat(s, "..");
}
static int pressCode(uint8_t id, int arg) { return id * 1000 + arg; }

// =====================================================================================
// hits

void KidsMesh::addHit(int x, int y, int w, int h, uint8_t id, int arg) {
  if (id == H_AUTHOR || id == H_FAILED) {   // message hits never reach under the bars
    if (y < BODY_TOP) { h -= BODY_TOP - y; y = BODY_TOP; }
    if (y + h > BODY_BOTTOM) h = BODY_BOTTOM - y;
  }
  if (_nhits >= MAX_HITS || w <= 0 || h <= 0) return;
  Hit& t = _hits[_nhits++];
  t.x = x; t.y = y; t.w = w; t.h = h; t.id = id; t.arg = arg;
}

int KidsMesh::hitTest(int x, int y) const {
  int best = -1;
  long best_area = 0;
  for (int i = 0; i < _nhits; i++) {
    const Hit& h = _hits[i];
    if (x >= h.x && x < h.x + h.w && y >= h.y && y < h.y + h.h) {
      long a = (long)h.w * h.h;
      if (best < 0 || a < best_area) { best = i; best_area = a; }
    }
  }
  if (best >= 0) return best;
  int best_d = HIT_TOL + 1;
  for (int i = 0; i < _nhits; i++) {
    const Hit& h = _hits[i];
    int dx = x < h.x ? h.x - x : (x >= h.x + h.w ? x - (h.x + h.w - 1) : 0);
    int dy = y < h.y ? h.y - y : (y >= h.y + h.h ? y - (h.y + h.h - 1) : 0);
    int d = dx > dy ? dx : dy;
    if (d < best_d) { best_d = d; best = i; }
  }
  return best;
}

bool KidsMesh::isPressed(uint8_t id, int arg) const { return _pressed == pressCode(id, arg); }

// =====================================================================================
// navigation

char* KidsMesh::composeBuf() {
  if (_screen == SCR_ROOM) return _draft_room;
  if (_screen == SCR_THREAD) return _draft_dm;
  if (_screen == SCR_PUBLIC) return _draft_pub;
  return _compose;
}
int KidsMesh::composeMax() const {
  if (_screen == SCR_NAME || _screen == SCR_RENAME) return spark::MEMBER_NAME_MAX;
  if (_screen == SCR_PIN) return 8;
  return COMPOSE_MAX;
}

void KidsMesh::go(Screen s) {
  if (_screen == SCR_SETTINGS && s != SCR_SETTINGS && s != SCR_TOUCHCAL) _edit = -1;
  _screen = s;
  _pressed = -1;
  _back_hint = 0;
  _list_px = 0;
  _sel = 0;
  _sel_visible = false;
  _empty_since = millis();
  _gest.swallow();
  _dirty = true;
}

void KidsMesh::goBack() {
  switch (_screen) {
    case SCR_ROOM:
    case SCR_NAME:
      hw.beepBump();
      _back_hint = 0;
      _dirty = true;
      return;
    case SCR_PEOPLE: go(SCR_ROOM); _scroll_px = 0; break;
    case SCR_THREAD:
    case SCR_PUBLIC: { Screen b = _back_to; go(b); if (b == SCR_ROOM) _scroll_px = 0; break; }
    case SCR_PIN: _compose[0] = 0; go(_pin_back); break;
    case SCR_SETTINGS:
      if (_edit >= 0) { _edit = -1; _compose[0] = 0; _dirty = true; _back_hint = 0; }
      else { _admin = false; go(_pin_back); }
      break;
    case SCR_MENU: go(SCR_PEOPLE); break;
    case SCR_RENAME: _compose[0] = 0; go(SCR_MENU); break;
    case SCR_CONFIRM: go(SCR_RENAME); break;
    case SCR_TOUCHCAL: go(SCR_SETTINGS); break;
  }
  hw.beepBack();
}

void KidsMesh::openThread(const uint8_t* peer6, Screen back_to) {
  if (memcmp(_draft_dm_peer, peer6, 6) != 0) {
    _draft_dm[0] = 0;   // the DM draft belongs to one person
    memcpy(_draft_dm_peer, peer6, 6);
  }
  memcpy(_thread_peer, peer6, 6);
  _back_to = back_to;
  _scroll_px = 0;
  go(SCR_THREAD);
}

void KidsMesh::openPublic(Screen back_to) {
  if (!publicOn()) return;
  _back_to = back_to;
  _scroll_px = 0;
  go(SCR_PUBLIC);
}

// People rows: 0 = the room, then Public (only while enabled), then the members.
void KidsMesh::openRow(int row) {
  if (row <= 0) { go(SCR_ROOM); _scroll_px = 0; return; }
  if (pubRows() && row == 1) { openPublic(SCR_PEOPLE); return; }
  int i = row - 1 - pubRows();
  if (i >= 0 && i < _num_people) openThread(_people[i].key, SCR_PEOPLE);
}

void KidsMesh::openPin(Screen back) {
  _compose[0] = 0;
  _pin_back = back;
  _pin_wrong = false;
  go(SCR_PIN);
}

// PIN entry: digits from the touch pad or the keyboard (also the letter keys that carry the
// digit on their keycap, for keyboards that only send digits with Alt held)
void KidsMesh::pinKey(char c) {
  static const char* KEYCAP = "w1e2r3s4d5f6z7x8c9";
  char lc = tolower(c);
  for (const char* p = KEYCAP; *p; p += 2) if (lc == p[0]) { c = p[1]; break; }
  int len = strlen(_compose);
  if (c >= '0' && c <= '9') {
    if (len < 8) { _compose[len] = c; _compose[len + 1] = 0; }
    _pin_wrong = false;
  } else if (c == KEY_BACKSPACE || c == 0x7F) {
    if (len > 0) _compose[len - 1] = 0;
    else goBack();
  } else if (c == KEY_ENTER) {
    submitCompose();
  }
  _dirty = true;
}

void KidsMesh::scrollBy(int dy) {
  if (chatScreen()) {
    _scroll_px += dy;              // finger down = older messages come into view
    if (_scroll_px < 0) _scroll_px = 0;
  } else if (_screen == SCR_PEOPLE || _screen == SCR_SETTINGS) {
    _list_px -= dy;
    if (_list_px < 0) _list_px = 0;
  }
  _dirty = true;
}

// =====================================================================================
// input

void KidsMesh::wake(unsigned long ms) {
  if (!hw.isBacklightOn()) {
    hw.backlight(true);
    _wake_until = millis() + ms;
    _last_input = millis() - SCREEN_IDLE_MS;   // off again after `ms` unless touched
  }
  _dirty = true;
}

void KidsMesh::applyCal() {
  if (_prefs.cal_ok) {
    _cal.x0 = _prefs.cal[0]; _cal.x1 = _prefs.cal[1]; _cal.y0 = _prefs.cal[2]; _cal.y1 = _prefs.cal[3];
  } else {
    _cal.x0 = TOUCH_CAL_X0; _cal.x1 = TOUCH_CAL_X1; _cal.y0 = TOUCH_CAL_Y0; _cal.y1 = TOUCH_CAL_Y1;
  }
}

void KidsMesh::calSample(uint8_t points, int16_t rx, int16_t ry) {
  if (points == 1) {
    _cal_sx += rx; _cal_sy += ry; _cal_n++;
    _cal_down_at = millis();
    return;
  }
  if (_cal_n == 0) return;
  // finger lifted: one calibration point
  _cal_raw[_cal_step * 2] = (int16_t)(_cal_sx / _cal_n);
  _cal_raw[_cal_step * 2 + 1] = (int16_t)(_cal_sy / _cal_n);
  _cal_sx = _cal_sy = 0; _cal_n = 0;
  _cal_step++;
  hw.beepKey();
  if (_cal_step == 2) {
    kids::TouchCal c;
    if (kids::touchSolve(CAL_PTS[0][0], CAL_PTS[0][1], _cal_raw[0], _cal_raw[1], CAL_PTS[1][0], CAL_PTS[1][1],
                         _cal_raw[2], _cal_raw[3], c)) {
      _prefs.cal[0] = c.x0; _prefs.cal[1] = c.x1; _prefs.cal[2] = c.y0; _prefs.cal[3] = c.y1;
      _prefs.cal_ok = 1;
      savePrefs();
      applyCal();
      _cal_msg = "Saved. Touch to test, < to finish";
      Serial.printf("touch cal x %d..%d y %d..%d\n", c.x0, c.x1, c.y0, c.y1);
    } else {
      _cal_msg = "That did not work, try again";
      _cal_step = 0;
    }
  }
  _dirty = true;
}

void KidsMesh::touchLoop() {
  uint8_t pts = 0;
  int16_t rx = 0, ry = 0;
  kids::UiEvent evs[2];
  int r = hw.touchPoll(pts, rx, ry);
  if (r == 1) {
    if (_touchraw) {
      int16_t sx, sy;
      kids::touchMap(_cal, rx, ry, sx, sy);
      Serial.printf("touch n=%u raw %d,%d -> %d,%d\n", pts, rx, ry, sx, sy);
    }
    if (pts) { _last_input = millis(); _wake_until = 0; }
    if (!hw.isBacklightOn()) {
      if (pts) {   // the first touch only wakes the screen
        hw.backlight(true);
        _dirty = true;
        _gest.feed(1, 0, 0, millis(), evs);
        _gest.swallow();
      }
      return;
    }
    if (_screen == SCR_TOUCHCAL && _cal_step < 2) { calSample(pts, rx, ry); return; }
    int16_t sx = 0, sy = 0;
    if (pts) kids::touchMap(_cal, rx, ry, sx, sy);
    int n = _gest.feed(pts, sx, sy, millis(), evs);
    for (int i = 0; i < n; i++) onEvent(evs[i]);
  }
  if (_screen == SCR_TOUCHCAL && _cal_step < 2 && _cal_n > 0 && millis() - _cal_down_at > 120) {
    calSample(0, 0, 0);   // released between polls
  }
  int n = _gest.tick(millis(), evs);
  for (int i = 0; i < n; i++) onEvent(evs[i]);
}

void KidsMesh::onEvent(const kids::UiEvent& e) {
  switch (e.type) {
    case kids::UI_PRESS: {
      int h = hitTest(e.x, e.y);
      _pressed = h >= 0 ? pressCode(_hits[h].id, _hits[h].arg) : -1;
      _sel_visible = false;
      if (_screen == SCR_TOUCHCAL) { _cal_dot_x = e.x; _cal_dot_y = e.y; }
      _dirty = true;
      break;
    }
    case kids::UI_CANCEL:
      _pressed = -1;
      _dirty = true;
      break;
    case kids::UI_TAP: {
      _pressed = -1;
      _dirty = true;
      int h = hitTest(e.x, e.y);
      if (h >= 0) {
        Hit hit = _hits[h];
        onTap(hit);
      }
      break;
    }
    case kids::UI_SCROLL:
      scrollBy(e.dy);
      break;
    case kids::UI_BACK_HINT:
      if (_screen != SCR_ROOM && _screen != SCR_NAME) { _back_hint = e.progress; _dirty = true; }
      break;
    case kids::UI_BACK:
      _back_hint = 0;
      goBack();
      break;
    default:
      break;
  }
}

void KidsMesh::onTap(const Hit& h) {
  switch (h.id) {
    case H_BACK: goBack(); return;
    case H_PEOPLE: go(SCR_PEOPLE); break;
    case H_SEND: case H_OK: case H_SAVE: submitCompose(); return;
    case H_ROW:
      if (_screen == SCR_PEOPLE) openRow(h.arg);
      else if (_screen == SCR_SETTINGS) settingsClick(h.arg);
      break;
    case H_AUTHOR: {
      Msg& m = _msgs[h.arg];
      Person* p = findPerson(m.author, 4);
      if (p) openThread(p->key, SCR_ROOM);
      break;
    }
    case H_FAILED:
      if (_msgs && _msgs[h.arg].id && transmit(_msgs[h.arg], 0)) _dirty = true;
      break;
    case H_NEWPILL: _scroll_px = 0; break;
    case H_YES:
      applyName(_new_name);
      go(SCR_PEOPLE);
      hw.beepSend();
      return;
    case H_NO: go(SCR_RENAME); break;
    case H_GEAR: go(SCR_MENU); break;
    case H_MENU_NAME: _compose[0] = 0; go(SCR_RENAME); break;
    case H_MENU_PARENT: openPin(SCR_PEOPLE); break;
    case H_DIGIT: pinKey('0' + h.arg); break;
    case H_DEL: pinKey(KEY_BACKSPACE); return;
    case H_VOL_DOWN: case H_VOL_UP:
      if (h.id == H_VOL_DOWN && _prefs.volume > 0) _prefs.volume--;
      if (h.id == H_VOL_UP && _prefs.volume < 3) _prefs.volume++;
      hw.setVolume(_prefs.volume);
      savePrefs();
      hw.beepDM();
      _dirty = true;
      return;
    default:
      return;
  }
  hw.beepKey();
}

void KidsMesh::uiLoop() {
  int c;
  while ((c = hw.readKey()) != 0) {
    bool was_off = !hw.isBacklightOn();
    _last_input = millis();
    _wake_until = 0;
    if (was_off) { hw.backlight(true); _dirty = true; continue; }   // first key only wakes
    onKey(c);
  }
  TrackEvent ev;
  while ((ev = hw.readTrackball()) != TB_NONE) {
    bool was_off = !hw.isBacklightOn();
    _last_input = millis();
    _wake_until = 0;
    if (was_off) { hw.backlight(true); _dirty = true; continue; }
    if (!_prefs.trackball_off) onTrack(ev);
  }
  touchLoop();

  if (hw.isBacklightOn() && (long)(millis() - _last_input) > (long)SCREEN_IDLE_MS &&
      (_wake_until == 0 || (long)(millis() - _wake_until) >= 0) && !_gest.down()) {
    hw.backlight(false);
  }
  // reading a thread at the bottom marks it read
  if (hw.isBacklightOn() && _scroll_px < 8) {
    if (_screen == SCR_ROOM) markRead(ZERO6);
    else if (_screen == SCR_THREAD) markRead(_thread_peer);
    else if (_screen == SCR_PUBLIC) markRead(PUBLIC6);
  }
  if (_screen == SCR_PUBLIC && !publicOn()) { go(SCR_ROOM); _scroll_px = 0; }   // turned off meanwhile
  if (_notice && (long)(millis() - _notice_until) >= 0) { _notice = NULL; _dirty = true; }
  unsigned long throttle = _gest.active() ? 33 : 80;
  if (_dirty && hw.isBacklightOn() && millis() - _last_draw > throttle) {
    _dirty = false;
    _last_draw = millis();
    draw();
  }
}

void KidsMesh::onKey(int c) {
  _dirty = true;
  _sel_visible = _screen == SCR_PEOPLE || (_screen == SCR_SETTINGS && _edit < 0);
  if (c == 0x0C) {   // Alt+C
    if (_screen == SCR_ROOM) go(SCR_PEOPLE); else goBack();
    return;
  }
  if (_screen == SCR_PIN) { pinKey((char)c); return; }
  bool composing = chatScreen() || _screen == SCR_NAME ||
                   _screen == SCR_RENAME || (_screen == SCR_SETTINGS && _edit >= 0);
  if (!composing) {
    if (c == KEY_ENTER) {
      if (_screen == SCR_PEOPLE) openRow(_sel);
      else if (_screen == SCR_SETTINGS) settingsClick(_sel);
      else if (_screen == SCR_CONFIRM) { Hit h = { 0, 0, 0, 0, H_YES, 0 }; onTap(h); }
      else if (_screen == SCR_MENU) { Hit h = { 0, 0, 0, 0, _sel == 0 ? H_MENU_NAME : H_MENU_PARENT, 0 }; onTap(h); }
    } else if (c == KEY_BACKSPACE || c == 0x7F) {
      goBack();
    } else if (_screen == SCR_CONFIRM && (c == 'y' || c == 'Y')) {
      Hit h = { 0, 0, 0, 0, H_YES, 0 }; onTap(h);
    } else if (_screen == SCR_CONFIRM && (c == 'n' || c == 'N')) {
      go(SCR_RENAME);
    } else if (_screen == SCR_PEOPLE && c > ' ' && c < 0x7F) {
      char lc = tolower(c);
      for (int i = 0; i < _num_people; i++) {
        if (tolower(_people[i].name[0]) == lc) { _sel = i + 1 + pubRows(); _list_px = _sel * ROW_H > 120 ? _sel * ROW_H - 120 : 0; break; }
      }
    }
    return;
  }
  char* buf = composeBuf();
  int len = strlen(buf);
  if (c == KEY_ENTER) {
    if (len == 0 && chatScreen()) { _scroll_px = 0; return; }
    submitCompose();
    return;
  }
  if (c == KEY_BACKSPACE || c == 0x7F) {
    if (len > 0) {
      buf[len - 1] = 0;
      if (len == 1) _empty_since = millis();
    } else if (_screen != SCR_ROOM && _screen != SCR_NAME && millis() - _empty_since >= BACKSPACE_BACK_MS) {
      goBack();
    }
    return;
  }
  if (c < 0x20 || c > 0x7E) return;
  if (len < composeMax()) { buf[len] = (char)c; buf[len + 1] = 0; }
}

void KidsMesh::onTrack(int ev) {
  _dirty = true;
  switch (_screen) {
    case SCR_ROOM:
    case SCR_THREAD:
    case SCR_PUBLIC: {
      const uint8_t* peer = _screen == SCR_ROOM ? ZERO6 : (_screen == SCR_PUBLIC ? PUBLIC6 : _thread_peer);
      if (ev == TB_UP) _scroll_px += 48;
      else if (ev == TB_DOWN) { _scroll_px -= 48; if (_scroll_px < 0) _scroll_px = 0; }
      else if (ev == TB_CLICK) { _scroll_px = 0; resendFailed(peer); }
      else if (ev == TB_RIGHT && _screen == SCR_ROOM) go(SCR_PEOPLE);
      else if (ev == TB_LEFT) goBack();
      break;
    }
    case SCR_PEOPLE:
    case SCR_SETTINGS: {
      if (_screen == SCR_SETTINGS && _edit >= 0) { if (ev == TB_LEFT) goBack(); break; }
      int rows = _screen == SCR_PEOPLE ? peopleRows() : settingsRows();
      int rh = _screen == SCR_PEOPLE ? ROW_H : SROW_H;
      _sel_visible = true;
      if (ev == TB_UP && _sel > 0) _sel--;
      else if (ev == TB_DOWN && _sel < rows - 1) _sel++;
      else if (ev == TB_LEFT) { goBack(); break; }
      else if (ev == TB_CLICK || ev == TB_RIGHT) {
        if (_screen == SCR_PEOPLE) openRow(_sel); else settingsClick(_sel);
        break;
      }
      // keep the focused row on screen
      int top = _sel * rh, view = SCREEN_H - BAR_H - (_edit >= 0 ? COMPOSE_H : 0);
      if (top < _list_px) _list_px = top;
      if (top + rh > _list_px + view) _list_px = top + rh - view;
      break;
    }
    case SCR_MENU:
      _sel_visible = true;
      if (ev == TB_UP) _sel = 0;
      else if (ev == TB_DOWN) _sel = 1;
      else if (ev == TB_LEFT) goBack();
      else if (ev == TB_CLICK || ev == TB_RIGHT) { Hit h = { 0, 0, 0, 0, _sel == 0 ? H_MENU_NAME : H_MENU_PARENT, 0 }; onTap(h); }
      break;
    default:
      if (ev == TB_LEFT) goBack();
      break;
  }
}

void KidsMesh::submitCompose() {
  char* buf = composeBuf();
  char text[COMPOSE_MAX + 1];
  strcpy(text, buf);
  char* s = text;
  while (*s == ' ') s++;
  int l = strlen(s);
  while (l > 0 && s[l - 1] == ' ') s[--l] = 0;

  switch (_screen) {
    case SCR_NAME:
      if (!*s) return;
      applyName(s);
      _compose[0] = 0;
      go(SCR_ROOM);
      hw.beepSend();
      return;
    case SCR_RENAME: {
      if (!*s) return;
      char tmp[16];
      spark::memberCopyName(tmp, s, strlen(s));
      if (!tmp[0] || strcmp(tmp, _prefs.name) == 0) return;
      strcpy(_new_name, tmp);
      go(SCR_CONFIRM);
      return;
    }
    case SCR_PIN:
      if (strlen(_compose) < 4) return;
      if (strcmp(_compose, _prefs.pin) == 0) { _compose[0] = 0; _admin = true; _edit = -1; go(SCR_SETTINGS); }
      else { _compose[0] = 0; _pin_wrong = true; _dirty = true; hw.beepFail(); }
      return;
    case SCR_SETTINGS:
      if (_edit >= 0) settingsSave(_edit, s);
      _edit = -1;
      _compose[0] = 0;
      _dirty = true;
      return;
    case SCR_ROOM:
    case SCR_THREAD:
      if (!*s) return;
      if (strcmp(s, "/admin") == 0 || strcmp(s, "/parents") == 0) {
        buf[0] = 0;
        openPin(_screen);
        return;
      }
      sendText(_screen == SCR_ROOM ? ZERO6 : _thread_peer, s);
      buf[0] = 0;
      _empty_since = millis();
      _scroll_px = 0;
      hw.beepSend();
      _dirty = true;
      return;
    case SCR_PUBLIC: {
      if (!*s) return;
      if (strcmp(s, "/admin") == 0 || strcmp(s, "/parents") == 0) { buf[0] = 0; openPin(_screen); return; }
      int before = _pub_head;
      sendPublic(s);
      if (_pub_head == before) { _dirty = true; return; }   // rate limited: the draft stays
      buf[0] = 0;
      _empty_since = millis();
      _scroll_px = 0;
      hw.beepSend();
      _dirty = true;
      return;
    }
    default:
      return;
  }
}

// =====================================================================================
// settings (parents)

void KidsMesh::settingsRow(int i, char* label, char* value) {
  value[0] = 0;
  switch (i) {
    case 0: strcpy(label, "Name"); strcpy(value, _prefs.name); break;
    case 1: strcpy(label, "Room password"); strcpy(value, _prefs.room_pw); break;
    case 2: strcpy(label, "Room key"); kids::toHex(value, _prefs.room_key, 4); strcat(value, "..."); break;
    case 3: strcpy(label, "Parent PIN"); strcpy(value, _prefs.pin); break;
    case 4: strcpy(label, "Volume"); sprintf(value, "%u", _prefs.volume); break;
    case 5: strcpy(label, "Share location"); strcpy(value, _prefs.share_loc ? "on (parents only)" : "off"); break;
    case 6: strcpy(label, "Trackball"); strcpy(value, _prefs.trackball_off ? "off" : "on"); break;
    case 7: strcpy(label, "Touch calibrate"); strcpy(value, hw.touchOk() ? (_prefs.cal_ok ? "calibrated" : "default") : "no touch"); break;
    case 8: strcpy(label, "Log in again"); strcpy(value, _link == LINK_ONLINE ? "online" : "offline"); break;
    case 9: strcpy(label, "Refresh people"); sprintf(value, "%d known", _num_people); break;
    case 10: {
      strcpy(label, "Info");
      char me[9];
      kids::toHex(me, self_id.pub_key, 4);
      sprintf(value, "%s %umV gps %s%ld touch %s", me, _batt_mv, gps.isValid() ? "s" : "-", gps.satellitesCount(),
              hw.touchOk() ? "ok" : "none");
      break;
    }
    case 11: strcpy(label, "Clear messages"); strcpy(value, "type YES"); break;
    case 12:
      strcpy(label, "Public channel");
      sprintf(value, "%s%s", publicOn() ? "on" : "off",
              _prefs.public_mode == kids::PUBLIC_DEFAULT ? " (build default)" : "");
      break;
    default: strcpy(label, "Lock settings"); strcpy(value, KIDS_FW_VERSION); break;
  }
}

void KidsMesh::settingsClick(int i) {
  switch (i) {
    case 0: case 1: case 3: case 11: {
      char label[24], value[80];
      settingsRow(i, label, value);
      _edit = i;
      StrHelper::strncpy(_compose, i == 11 ? "" : value, sizeof(_compose));
      break;
    }
    case 2: _edit = 2; kids::toHex(_compose, _prefs.room_key, 32); break;
    case 4: _prefs.volume = (_prefs.volume + 1) % 4; hw.setVolume(_prefs.volume); savePrefs(); hw.beepDM(); break;
    case 5: _prefs.share_loc = !_prefs.share_loc; savePrefs(); break;
    case 6: _prefs.trackball_off = !_prefs.trackball_off; savePrefs(); break;
    case 7:
      _cal_step = 0; _cal_n = 0; _cal_sx = _cal_sy = 0; _cal_dot_x = _cal_dot_y = -1;
      _cal_msg = hw.touchOk() ? "Tap the middle of the cross" : "No touch screen found";
      go(SCR_TOUCHCAL);
      break;
    case 8: { ContactInfo* r = room(); if (r) resetPathTo(*r); _login_fails = 0; _next_login = millis(); _link = LINK_OFFLINE; break; }
    case 9: _members_next = 0; _members_tag = 0; _next_members = millis(); break;
    case 10: break;
    case 12: _prefs.public_mode = kids::publicToggled(_prefs.public_mode, KIDS_PUBLIC_ENABLED); savePrefs(); break;
    default: _admin = false; go(SCR_ROOM); return;
  }
  _dirty = true;
}

void KidsMesh::settingsSave(int i, const char* v) {
  switch (i) {
    case 0: if (*v) applyName(v); break;
    case 1: if (*v) StrHelper::strncpy(_prefs.room_pw, v, sizeof(_prefs.room_pw)); _next_login = millis(); break;
    case 2: {
      uint8_t key[32];
      if (kids::parseKey(v, key) && memcmp(key, _prefs.room_key, 32) != 0) {
        ContactInfo* r = room();
        if (r) removeContact(*r);
        memcpy(_prefs.room_key, key, 32);
        _prefs.room_sync_since = 0;
        setupRoomContact();
        _next_login = millis();
      }
      break;
    }
    case 3: {   // 4..8 digits (the PIN pad only has digits)
      int l = strlen(v);
      bool ok = l >= 4 && l <= 8;
      for (int k = 0; ok && k < l; k++) ok = v[k] >= '0' && v[k] <= '9';
      if (ok) StrHelper::strncpy(_prefs.pin, v, sizeof(_prefs.pin));
      break;
    }
    case 11:
      if (strcmp(v, "YES") == 0 && _msgs) {
        memset(_msgs, 0, MSG_CAP * sizeof(Msg));
        _msg_head = 0;
        saveMsgs();
      }
      break;
  }
  savePrefs();
}

// =====================================================================================
// drawing

static void printClipped(FrameBuffer& fb, const char* s, int len) {
  char tmp[200];
  if (len > (int)sizeof(tmp) - 1) len = sizeof(tmp) - 1;
  memcpy(tmp, s, len);
  tmp[len] = 0;
  fb.print(tmp);
}

void KidsMesh::drawButton(int x, int y, int w, int h, const char* label, uint8_t id, bool enabled, uint16_t fill) {
  FrameBuffer& fb = hw.fb;
  bool down = isPressed(id);
  uint16_t bg = !enabled ? C_DGREY : (down ? C_LGREY : fill);
  fb.fillRoundRect(x, y, w, h, 8, bg);
  if (down) fb.drawRoundRect(x, y, w, h, 8, C_WHITE);
  fb.setFont(&FreeSansBold9pt7b);
  fb.setTextColor(enabled ? (down ? C_BLACK : C_WHITE) : C_GREY);
  int tw = textW(label, &FreeSansBold9pt7b);
  fb.setCursor(x + (w - tw) / 2, y + h / 2 + 6);
  fb.print(label);
}

void KidsMesh::drawGear(int cx, int cy, uint16_t col) {
  FrameBuffer& fb = hw.fb;
  for (int i = 0; i < 8; i++) {   // eight teeth
    float a = i * (float)M_PI / 4;
    fb.fillCircle(cx + (int)lroundf(10 * cosf(a)), cy + (int)lroundf(10 * sinf(a)), 3, col);
  }
  fb.fillCircle(cx, cy, 9, col);
  fb.fillCircle(cx, cy, 4, C_BAR);
}

void KidsMesh::drawTopBar(const char* title, bool back, bool people_btn, bool gear) {
  FrameBuffer& fb = hw.fb;
  fb.fillRect(0, 0, SCREEN_W, BAR_H, C_BAR);
  int x = 8;
  if (back) {
    drawButton(4, 4, 48, 32, "<", H_BACK, true, C_SEL);
    addHit(0, 0, 72, 48, H_BACK);
    x = 62;
  }
  // right side: People button (room), battery, clock, GPS
  int right = people_btn ? 212 : (gear ? 264 : SCREEN_W - 4);
  if (gear) {   // People: Change my name / Parent settings
    bool down = isPressed(H_GEAR);
    fb.fillRoundRect(270, 4, 46, 32, 8, down ? C_LGREY : C_SEL);
    drawGear(293, 20, down ? C_BLACK : C_WHITE);
    if (down) fb.fillCircle(293, 20, 4, C_LGREY);
    addHit(264, 0, 56, 48, H_GEAR);
  }
  if (people_btn) {
    drawButton(216, 4, 100, 32, "People", H_PEOPLE, true, C_SEL);
    addHit(212, 0, 108, 48, H_PEOPLE);
    int dms = unreadDMs();
    if (dms > 0) {
      char b[8];
      sprintf(b, "%d", dms > 99 ? 99 : dms);
      fb.setFont(&FreeSansBold9pt7b);
      int w = textW(b, &FreeSansBold9pt7b) + 10;
      fb.fillRoundRect(SCREEN_W - w - 2, 0, w, 18, 9, C_ORANGE);
      fb.setTextColor(C_BLACK);
      fb.setCursor(SCREEN_W - w + 3, 14);
      fb.print(b);
    }
  }
  int bars = kids::batteryBars(_batt_mv);
  int bx = right - 30;
  uint16_t bc = bars == 0 ? C_RED : C_WHITE;
  fb.drawRect(bx, 13, 24, 14, bc);
  fb.fillRect(bx + 24, 17, 3, 6, bc);
  for (int i = 0; i < bars; i++) fb.fillRect(bx + 2 + i * 5, 15, 4, 10, bars == 1 ? C_ORANGE : C_GREEN);
  if (_batt_mv > 4250) { fb.setFont(&FreeSansBold9pt7b); fb.setTextColor(C_YELLOW); fb.setCursor(bx + 7, 26); fb.print("+"); }

  uint32_t now = getRTCClock()->getCurrentTime();
  char clk[8] = "--:--";
  if (now > 1700000000UL) {
    uint32_t local = now + kids::tzOffset(_tz, now);
    sprintf(clk, "%02u:%02u", (unsigned)((local / 3600) % 24), (unsigned)((local / 60) % 60));
  }
  fb.setFont(&FreeSansBold9pt7b);
  fb.setTextColor(C_WHITE);
  int cw = textW(clk, &FreeSansBold9pt7b);
  int cx = bx - 6 - cw;
  fb.setCursor(cx, 26);
  fb.print(clk);
  int gx = cx - 12;
  if (_prefs.share_loc) {
    uint16_t gc = gps.isValid() ? C_GREEN : C_DGREY;
    fb.fillCircle(gx, 20, 3, gc);
    fb.drawCircle(gx, 20, 6, gc);
    gx -= 10;
  }

  // title + link dot
  char t[40];
  kids::foldUtf8(title, t, sizeof(t));
  int maxw = gx - 14 - x - 14;
  fitText(t, &FreeSansBold9pt7b, maxw);
  fb.setTextColor(C_WHITE);
  fb.setCursor(x, 26);
  fb.print(t);
  int dx = x + textW(t, &FreeSansBold9pt7b) + 10;
  uint16_t dot = _link == LINK_ONLINE ? C_GREEN : (_link == LINK_LOGIN ? C_YELLOW : C_RED);
  fb.fillCircle(dx, 20, 5, dot);

  if (bars == 0 && _batt_mv > 0) {
    fb.fillRect(0, BAR_H, SCREEN_W, 20, C_RED);
    fb.setTextColor(C_WHITE);
    fb.setCursor(110, BAR_H + 15);
    fb.print("Charge me!");
  }
}

void KidsMesh::drawCompose(const char* prompt, bool mask, uint8_t action_id, const char* action_label) {
  FrameBuffer& fb = hw.fb;
  int y0 = BODY_BOTTOM;
  const char* buf = chatScreen() ? composeBuf() : _compose;
  fb.fillRect(0, y0, SCREEN_W, COMPOSE_H, C_BAR);
  fb.drawFastHLine(0, y0, SCREEN_W, C_GREY);
  int text_right = action_id ? 248 : SCREEN_W - 8;
  if (action_id) {
    bool enabled = buf[0] != 0;
    drawButton(254, y0 + 5, 62, 34, action_label, action_id, enabled, C_BLUE);
    if (enabled) addHit(248, y0, 72, COMPOSE_H, action_id);
  }
  fb.setFont(&FreeSans12pt7b);
  char shown[COMPOSE_MAX + 2];
  if (mask) { int l = strlen(buf); memset(shown, '*', l); shown[l] = 0; }
  else kids::foldUtf8(buf, shown, sizeof(shown) - 1);
  if (!shown[0] && prompt) {
    char p[60];
    StrHelper::strncpy(p, prompt, sizeof(p));
    fitText(p, &FreeSans12pt7b, text_right - 16);
    fb.setTextColor(C_GREY);
    fb.setCursor(8, y0 + 29);
    fb.print(p);
    return;
  }
  if ((millis() / 500) & 1) strcat(shown, "_"); else strcat(shown, " ");
  int left = chatScreen() ? COMPOSE_MAX - (int)strlen(buf) : 99;
  int maxw = text_right - 8 - (left < 40 ? 28 : 0);
  const char* p = shown;
  while (*p && textW(p, &FreeSans12pt7b) > maxw) p++;
  fb.setTextColor(C_WHITE);
  fb.setCursor(8, y0 + 29);
  fb.print(p);
  if (left < 40) {
    char cnt[8];
    sprintf(cnt, "%d", left);
    fb.setFont(&FreeSans9pt7b);
    fb.setTextColor(left < 10 ? C_ORANGE : C_GREY);
    fb.setCursor(text_right - 4 - textW(cnt, &FreeSans9pt7b), y0 + 28);
    fb.print(cnt);
  }
  _dirty = true;   // blinking cursor
}

void KidsMesh::drawNotice() {
  if (!_notice) return;
  FrameBuffer& fb = hw.fb;
  fb.setFont(&FreeSansBold9pt7b);
  int w = textW(_notice, &FreeSansBold9pt7b) + 24;
  int x = (SCREEN_W - w) / 2, y = BODY_BOTTOM - 36;
  fb.fillRoundRect(x, y, w, 30, 15, C_ORANGE);
  fb.setTextColor(C_BLACK);
  fb.setCursor(x + 12, y + 20);
  fb.print(_notice);
}

void KidsMesh::drawBackHint() {
  if (_back_hint == 0) return;
  FrameBuffer& fb = hw.fb;
  int cx = SCREEN_W - 30, cy = 118;
  bool ready = _back_hint >= 100;
  fb.fillCircle(cx, cy, 20, ready ? C_GREEN : C_DGREY);
  if (!ready) fb.fillCircle(cx, cy, 2 + 16 * _back_hint / 100, C_GREY);
  fb.setFont(&FreeSansBold9pt7b);
  fb.setTextColor(C_WHITE);
  fb.setCursor(cx - 5, cy + 6);
  fb.print("<");
}

struct WrapCtx { const char* lines[8]; int lens[8]; int n; };
static void wrap_cb(const char* s, int len, void* ctx) {
  WrapCtx* w = (WrapCtx*)ctx;
  if (w->n < 8) { w->lines[w->n] = s; w->lens[w->n] = len; w->n++; }
}

void KidsMesh::drawThread(const uint8_t* peer6) {
  FrameBuffer& fb = hw.fb;
  bool pub_view = isPublicPeer(peer6);
  Msg* ring = pub_view ? _pub : _msgs;
  int cap = pub_view ? PUB_CAP : MSG_CAP;
  int head = pub_view ? _pub_head : _msg_head;
  if (!ring) return;
  bool room_view = isRoomPeer(peer6);
  int y = BODY_BOTTOM - 4 + _scroll_px;
  int drawn = 0, unread_below = 0;
  bool above = false;
  int oldest_top = BODY_BOTTOM;
  static char folded[200];
  for (int k = 1; k <= cap; k++) {
    int idx = (head - k + cap) % cap;
    Msg& m = ring[idx];
    if (!m.id || memcmp(m.peer, peer6, 6) != 0) continue;
    bool own = m.flags & MSG_OUT;
    char pname[16];
    const char* body = m.text;
    if (pub_view && !own) body = kids::splitChannelText(m.text, pname, sizeof(pname));   // "<name>: <text>"
    kids::foldUtf8(body, folded, sizeof(folded));
    WrapCtx w;
    w.n = 0;
    int maxw = own ? SCREEN_W - 60 : SCREEN_W - 16;
    kids::wrapText(folded, maxw, glyphW, (void*)&FreeSans12pt7b, wrap_cb, &w);
    bool failed = own && (m.flags & MSG_FAILED);
    int h = HDR_H + w.n * LINE_H + 8;
    y -= h;
    oldest_top = y;
    drawn++;
    if (y >= BODY_BOTTOM) { if (m.flags & MSG_UNREAD) unread_below++; continue; }   // below the view
    if (y + h <= BODY_TOP) { above = true; break; }

    char nb[12], name[40];
    kids::foldUtf8(pub_view && !own ? (pname[0] ? pname : "?") : authorName(m.author, nb), name, 20);
    fitText(name, &FreeSansBold9pt7b, 220);
    uint16_t col = own ? C_OWN : AUTHOR_COLORS[kids::authorColor(m.author, 8)];
    if (memcmp(m.author, _prefs.room_key, 4) == 0) col = C_LGREY;
    char tm[8] = "";
    if (m.ts > 1700000000UL) {
      uint32_t local = m.ts + kids::tzOffset(_tz, m.ts);
      sprintf(tm, "%02u:%02u", (unsigned)((local / 3600) % 24), (unsigned)((local / 60) % 60));
    }
    int hy = y + 15;
    fb.setFont(&FreeSansBold9pt7b);
    if (own) {
      int tw = textW(tm, &FreeSansBold9pt7b);
      int sx = SCREEN_W - 8 - 14;
      if (failed && pub_view) {   // Public posts are not retried
        fb.setTextColor(C_RED);
        const char* t = "not sent  !";
        fb.setCursor(SCREEN_W - 8 - textW(t, &FreeSansBold9pt7b), hy);
        fb.print(t);
      } else if (failed) {
        bool down = isPressed(H_FAILED, idx);
        if (down) fb.fillRect(0, y, SCREEN_W, h, C_SEL);
        fb.setTextColor(C_RED);
        const char* t = "tap to retry  !";
        fb.setCursor(SCREEN_W - 8 - textW(t, &FreeSansBold9pt7b), hy);
        fb.print(t);
        addHit(0, y, SCREEN_W, h, H_FAILED, idx);
      } else {
        fb.setTextColor(C_GREY);
        fb.setCursor(sx - 6 - tw, hy);
        fb.print(tm);
        if (m.flags & MSG_ACKED) {
          fb.drawLine(sx, hy - 5, sx + 4, hy - 1, C_GREEN); fb.drawLine(sx + 4, hy - 1, sx + 12, hy - 10, C_GREEN);
          fb.drawLine(sx, hy - 4, sx + 4, hy, C_GREEN); fb.drawLine(sx + 4, hy, sx + 12, hy - 9, C_GREEN);
        } else {
          fb.fillCircle(sx + 2, hy - 4, 1, C_GREY); fb.fillCircle(sx + 6, hy - 4, 1, C_GREY); fb.fillCircle(sx + 10, hy - 4, 1, C_GREY);
        }
      }
    } else {
      // in the room, a member's name line opens a DM with them
      bool tappable = room_view && memcmp(m.author, _prefs.room_key, 4) != 0 && findPerson(m.author, 4);
      if (tappable) {
        if (isPressed(H_AUTHOR, idx)) fb.fillRect(0, y, SCREEN_W, HDR_H + 2, C_SEL);
        addHit(0, y - 2, SCREEN_W, HDR_H + 6, H_AUTHOR, idx);
      }
      fb.setTextColor(col);
      fb.setCursor(8, hy);
      fb.print(name);
      int nx = 8 + textW(name, &FreeSansBold9pt7b);
      if (tappable) { fb.drawFastHLine(8, hy + 3, nx - 8, col); }
      fb.setTextColor(C_GREY);
      fb.setCursor(nx + 8, hy);
      fb.print(tm);
      if (m.flags & MSG_UNREAD) fb.fillCircle(SCREEN_W - 12, hy - 5, 4, C_ORANGE);
    }
    fb.setFont(&FreeSans12pt7b);
    fb.setTextColor(failed ? C_RED : (own ? C_OWN : C_WHITE));
    for (int i = 0; i < w.n; i++) {
      int ly = y + HDR_H + i * LINE_H + 18;
      int lx = 8;
      if (own) {
        char tmp[200];
        int l = w.lens[i] < 199 ? w.lens[i] : 199;
        memcpy(tmp, w.lines[i], l); tmp[l] = 0;
        lx = SCREEN_W - 8 - textW(tmp, &FreeSans12pt7b);
      }
      fb.setCursor(lx, ly);
      printClipped(fb, w.lines[i], w.lens[i]);
    }
  }
  // clamp: never scroll the oldest message further down than the top of the body
  if (!above && _scroll_px > 0 && oldest_top > BODY_TOP + 8) {
    _scroll_px -= oldest_top - (BODY_TOP + 8);
    if (_scroll_px < 0) _scroll_px = 0;
    _dirty = true;
  }
  if (drawn == 0) {
    fb.setFont(&FreeSans12pt7b);
    fb.setTextColor(C_GREY);
    fb.setCursor(20, 120);
    fb.print(pub_view ? "Nothing on Public yet" : (room_view ? "No messages yet" : "Say hello!"));
  }
  if (_scroll_px >= 8) {
    char b[24];
    int nu = unreadFor(peer6);
    if (nu) sprintf(b, "v %d new", nu); else strcpy(b, "v newest");
    fb.setFont(&FreeSansBold9pt7b);
    int w = textW(b, &FreeSansBold9pt7b) + 20;
    if (w < 112) w = 112;
    int px = SCREEN_W - w - 8, py = BODY_BOTTOM - 38;
    fb.fillRoundRect(px, py, w, 32, 16, isPressed(H_NEWPILL) ? C_LGREY : (nu ? C_ORANGE : C_DGREY));
    fb.setTextColor(nu ? C_BLACK : C_WHITE);
    fb.setCursor(px + (w - textW(b, &FreeSansBold9pt7b)) / 2, py + 21);
    fb.print(b);
    addHit(px, py, w, 32, H_NEWPILL);
  }
  (void)unread_below;
}

void KidsMesh::drawPeople() {
  FrameBuffer& fb = hw.fb;
  int rows = peopleRows();
  int view = SCREEN_H - BAR_H;
  int maxpx = rows * ROW_H - view;
  if (maxpx < 0) maxpx = 0;
  if (_list_px > maxpx) _list_px = maxpx;
  for (int r = 0; r < rows; r++) {
    int y = BAR_H + r * ROW_H - _list_px;
    if (y + ROW_H <= BAR_H || y >= SCREEN_H) continue;
    if (isPressed(H_ROW, r) || (_sel_visible && _sel == r)) fb.fillRect(0, y, SCREEN_W, ROW_H - 4, C_SEL);
    fb.drawFastHLine(8, y + ROW_H - 2, SCREEN_W - 16, C_DGREY);
    int top = y < BAR_H ? BAR_H : y;
    addHit(0, top, SCREEN_W, y + ROW_H - top, H_ROW, r);

    char name[40], initial[3] = "?";
    int unread = 0;
    uint16_t col;
    const char* sub = NULL;
    int pi = r - 1 - pubRows();
    if (r == 0) {
      snprintf(name, sizeof(name), "%s room", KIDS_ROOM_NAME);
      strcpy(initial, "#");
      unread = unreadFor(ZERO6);
      col = C_LGREY;
    } else if (pubRows() && r == 1) {
      strcpy(name, "Public");
      strcpy(initial, "P");
      unread = unreadFor(PUBLIC6);
      col = C_ORANGE;
      sub = "everyone nearby can read it";
    } else if (pi >= 0 && pi < _num_people) {
      Person& p = _people[pi];
      char nb[12];
      kids::foldUtf8(p.name[0] ? p.name : (kids::toHex(nb, p.key, 4), nb), name, 20);
      initial[0] = toupper(name[0]); initial[1] = 0;
      unread = unreadFor(p.key);
      col = AUTHOR_COLORS[kids::authorColor(p.key, 8)];
      if (p.flags & spark::MEMBER_FLAG_ADMIN) sub = "parent";
    } else {
      continue;
    }
    int cy = y + (ROW_H - 4) / 2;
    fb.fillCircle(28, cy, 18, col);
    fb.setFont(&FreeSansBold9pt7b);
    fb.setTextColor(C_BLACK);
    fb.setCursor(28 - textW(initial, &FreeSansBold9pt7b) / 2, cy + 6);
    fb.print(initial);
    // name on one line; a sub line (parent / current name) below it, both fitted to the row
    int text_right = SCREEN_W - (unread > 0 ? 56 : 10);
    bool two = sub && sub[0];
    fitText(name, &FreeSans12pt7b, text_right - 58);
    fb.setFont(&FreeSans12pt7b);
    fb.setTextColor(C_WHITE);
    fb.setCursor(58, two ? cy - 1 : cy + 8);
    fb.print(name);
    if (two) {
      char sb[40];
      kids::foldUtf8(sub, sb, sizeof(sb));
      fitText(sb, &FreeSans9pt7b, text_right - 58);
      fb.setFont(&FreeSans9pt7b);
      fb.setTextColor(C_GREY);
      fb.setCursor(58, cy + 17);
      fb.print(sb);
    }
    if (unread > 0) {
      char b[8];
      sprintf(b, "%d", unread > 99 ? 99 : unread);
      fb.setFont(&FreeSansBold9pt7b);
      int w = textW(b, &FreeSansBold9pt7b) + 16;
      if (w < 26) w = 26;
      fb.fillRoundRect(SCREEN_W - w - 10, cy - 12, w, 24, 12, C_ORANGE);
      fb.setTextColor(C_BLACK);
      fb.setCursor(SCREEN_W - w - 10 + (w - textW(b, &FreeSansBold9pt7b)) / 2, cy + 6);
      fb.print(b);
    }
  }
  if (_num_people == 0) {   // below the last row, never on top of it
    const char* line;
    if (_link != LINK_ONLINE) line = "Waiting for " KIDS_ROOM_NAME "...";
    else if (_members_state == MEMBERS_OK) line = "No one else here yet";
    else if (_members_state == MEMBERS_FAILED) line = KIDS_ROOM_NAME " did not answer, trying again";
    else line = "Loading people...";
    fb.setFont(&FreeSans9pt7b);
    fb.setTextColor(C_GREY);
    fb.setCursor(20, BAR_H + rows * ROW_H + 28 - _list_px);
    fb.print(line);
  }
}

void KidsMesh::drawSettings() {
  FrameBuffer& fb = hw.fb;
  int rows = settingsRows();
  int area_bottom = _edit >= 0 ? BODY_BOTTOM : SCREEN_H;
  int view = area_bottom - BAR_H;
  int maxpx = rows * SROW_H - view;
  if (maxpx < 0) maxpx = 0;
  if (_list_px > maxpx) _list_px = maxpx;
  for (int r = 0; r < rows; r++) {
    int y = BAR_H + r * SROW_H - _list_px;
    if (y + SROW_H <= BAR_H || y >= area_bottom) continue;
    bool hl = isPressed(H_ROW, r) || (_sel_visible && _sel == r) || _edit == r;
    if (hl) fb.fillRect(0, y, SCREEN_W, SROW_H - 2, C_SEL);
    fb.drawFastHLine(0, y + SROW_H - 1, SCREEN_W, C_DGREY);
    char label[24], value[80], fv[80];
    settingsRow(r, label, value);
    fb.setFont(&FreeSansBold9pt7b);
    fb.setTextColor(C_WHITE);
    fb.setCursor(8, y + 27);
    fitText(label, &FreeSansBold9pt7b, (r == 4 ? 140 : 130) - 8);
    fb.print(label);
    if (r == 4) {   // volume: - and + buttons
      drawButton(150, y + 4, 48, 36, "-", H_VOL_DOWN, true, C_DGREY);
      addHit(146, y, 56, SROW_H, H_VOL_DOWN);
      fb.setTextColor(C_WHITE);
      fb.setCursor(218, y + 27);
      fb.print(value);
      drawButton(250, y + 4, 48, 36, "+", H_VOL_UP, true, C_DGREY);
      addHit(246, y, 60, SROW_H, H_VOL_UP);
      continue;
    }
    int top = y < BAR_H ? BAR_H : y;
    addHit(0, top, SCREEN_W, y + SROW_H - top, H_ROW, r);
    fb.setFont(&FreeSans9pt7b);
    fb.setTextColor(C_LGREY);
    kids::foldUtf8(value, fv, sizeof(fv));
    int x = 140;
    fitText(fv, &FreeSans9pt7b, SCREEN_W - x - 6);
    fb.setCursor(x, y + 27);
    fb.print(fv);
  }
  if (_edit >= 0) drawCompose("type, then Save", false, H_SAVE, "Save");
}

void KidsMesh::drawPrompt(const char* line1, const char* line2, const char* hint) {
  FrameBuffer& fb = hw.fb;
  fb.setFont(&FreeSans12pt7b);
  fb.setTextColor(C_WHITE);
  fb.setCursor(20, 85);
  fb.print(line1);
  if (line2 && line2[0]) {
    char t[40];
    kids::foldUtf8(line2, t, sizeof(t));
    fitText(t, &FreeSans12pt7b, SCREEN_W - 28);
    fb.setCursor(20, 120);
    fb.print(t);
  }
  fb.setFont(&FreeSans9pt7b);
  fb.setTextColor(C_GREY);
  fb.setCursor(20, 160);
  fb.print(hint);
}

void KidsMesh::drawConfirm() {
  FrameBuffer& fb = hw.fb;
  char t[40];
  kids::foldUtf8(_new_name, t, sizeof(t));
  fitText(t, &FreeSans12pt7b, SCREEN_W - 40 - 12);
  fb.setFont(&FreeSans12pt7b);
  fb.setTextColor(C_WHITE);
  fb.setCursor(20, 80);
  fb.print("Change your name to");
  fb.setTextColor(C_YELLOW);
  fb.setCursor(20, 115);
  fb.print(t);
  fb.print("?");
  drawButton(20, 150, 130, 56, "Yes", H_YES, true, 0x0400);
  addHit(12, 142, 146, 72, H_YES);
  drawButton(170, 150, 130, 56, "No", H_NO, true, C_DGREY);
  addHit(162, 142, 146, 72, H_NO);
}

void KidsMesh::drawMenu() {
  static const char* LABELS[2] = { "Change my name", "Parent settings" };
  static const uint8_t IDS[2] = { H_MENU_NAME, H_MENU_PARENT };
  FrameBuffer& fb = hw.fb;
  for (int i = 0; i < 2; i++) {
    int y = BAR_H + 16 + i * 72;
    bool hl = isPressed(IDS[i]) || (_sel_visible && _sel == i);
    fb.fillRoundRect(16, y, SCREEN_W - 32, 60, 10, hl ? C_LGREY : C_SEL);
    fb.setFont(&FreeSans12pt7b);
    fb.setTextColor(hl ? C_BLACK : C_WHITE);
    fb.setCursor(32, y + 28);
    fb.print(LABELS[i]);
    fb.setFont(&FreeSans9pt7b);
    fb.setTextColor(hl ? C_DGREY : C_GREY);
    fb.setCursor(32, y + 49);
    if (i == 0) {
      char t[40];
      kids::foldUtf8(_prefs.name, t, sizeof(t));
      fitText(t, &FreeSans9pt7b, 180);
      fb.print("Now: ");
      fb.print(t);
    } else {
      fb.print("For mum and dad (PIN)");
    }
    addHit(8, y - 6, SCREEN_W - 16, 72, IDS[i]);
  }
}

void KidsMesh::drawPinPad() {
  FrameBuffer& fb = hw.fb;
  // left: prompt and the digits typed so far (as dots)
  fb.setFont(&FreeSansBold9pt7b);
  fb.setTextColor(C_WHITE);
  fb.setCursor(10, 80);
  fb.print("Parent PIN");
  int len = strlen(_compose);
  for (int i = 0; i < 8; i++) {
    int x = 16 + (i % 4) * 22, y = 110 + (i / 4) * 24;
    if (i < len) fb.fillCircle(x, y, 7, C_WHITE);
    else if (i < 4) fb.drawCircle(x, y, 7, C_GREY);
  }
  if (_pin_wrong) {
    fb.setFont(&FreeSans9pt7b);
    fb.setTextColor(C_RED);
    fb.setCursor(10, 180);
    fb.print("Wrong PIN");
  }
  // right: 3 x 4 pad
  static const char* KEYS[12] = { "1", "2", "3", "4", "5", "6", "7", "8", "9", "Del", "0", "OK" };
  const int kw = 64, kh = 44, gx = 4, gy = 4, x0 = SCREEN_W - 3 * kw - 2 * gx - 4, y0 = BAR_H + 6;
  for (int i = 0; i < 12; i++) {
    int col = i % 3, row = i / 3;
    int x = x0 + col * (kw + gx), y = y0 + row * (kh + gy);
    uint8_t id = i == 9 ? H_DEL : (i == 11 ? H_OK : H_DIGIT);
    int arg = id == H_DIGIT ? (i == 10 ? 0 : i + 1) : 0;
    bool enabled = id != H_OK || len >= 4;
    bool down = isPressed(id, arg);
    uint16_t fill = id == H_OK ? C_BLUE : (id == H_DEL ? C_DGREY : C_SEL);
    fb.fillRoundRect(x, y, kw, kh, 8, !enabled ? C_DGREY : (down ? C_LGREY : fill));
    const GFXfont* f = id == H_DIGIT ? &FreeSans12pt7b : &FreeSansBold9pt7b;
    fb.setFont(f);
    fb.setTextColor(enabled ? (down ? C_BLACK : C_WHITE) : C_GREY);
    fb.setCursor(x + (kw - textW(KEYS[i], f)) / 2, y + kh / 2 + (id == H_DIGIT ? 8 : 6));
    fb.print(KEYS[i]);
    if (enabled) addHit(x - gx / 2, y - gy / 2, kw + gx, kh + gy, id, arg);
  }
}

void KidsMesh::drawTouchCal() {
  FrameBuffer& fb = hw.fb;
  if (_cal_step < 2) {
    int cx = CAL_PTS[_cal_step][0], cy = CAL_PTS[_cal_step][1];
    fb.drawFastHLine(cx - 14, cy, 29, C_WHITE);
    fb.drawFastVLine(cx, cy - 14, 29, C_WHITE);
    fb.drawCircle(cx, cy, 6, C_ORANGE);
    fb.setFont(&FreeSans12pt7b);
    fb.setTextColor(C_WHITE);
    fb.setCursor(60, 115);
    fb.print(_cal_step == 0 ? "Tap the cross (1 of 2)" : "Tap the cross (2 of 2)");
    if (_cal_msg) {
      fb.setFont(&FreeSans9pt7b);
      fb.setTextColor(C_GREY);
      fb.setCursor(60, 145);
      fb.print(_cal_msg);
    }
    return;
  }
  // free test: 3 x 3 targets and a dot where the finger lands
  for (int i = 0; i < 3; i++) {
    for (int j = 0; j < 3; j++) {
      int x = 30 + j * 110, y = 60 + i * 60;
      bool hit = _cal_dot_x >= x - 25 && _cal_dot_x <= x + 25 && _cal_dot_y >= y - 20 && _cal_dot_y <= y + 20;
      fb.drawRoundRect(x - 25, y - 20, 50, 40, 6, hit ? C_GREEN : C_DGREY);
    }
  }
  if (_cal_dot_x >= 0) fb.fillCircle(_cal_dot_x, _cal_dot_y, 4, C_YELLOW);
  if (_cal_msg) {
    fb.setFont(&FreeSans9pt7b);
    fb.setTextColor(C_GREY);
    fb.setCursor(8, SCREEN_H - 8);
    fb.print(_cal_msg);
  }
}

void KidsMesh::draw() {
  FrameBuffer& fb = hw.fb;
  fb.fillScreen(C_BLACK);
  fb.setTextWrap(false);   // never continue a long label on the next line
  _nhits = 0;
  switch (_screen) {
    case SCR_NAME:
      drawPrompt("Hello!", "What is your name?", "Type it, then OK");
      drawCompose("your name", false, H_OK, "OK");
      drawTopBar(KIDS_ROOM_NAME, false, false);
      break;
    case SCR_ROOM:
      drawThread(ZERO6);
      drawCompose(_link == LINK_ONLINE ? "write to everyone..." : "write (sends later)...", false, H_SEND, "Send");
      drawTopBar(KIDS_ROOM_NAME, false, true);
      break;
    case SCR_PUBLIC:
      drawThread(PUBLIC6);
      drawNotice();
      drawCompose("write to Public...", false, H_SEND, "Send");
      drawTopBar("Public", true, false);
      drawBackHint();
      break;
    case SCR_THREAD: {
      drawThread(_thread_peer);
      drawCompose("write a message...", false, H_SEND, "Send");
      Person* p = findPerson(_thread_peer, 6);
      char nb[12];
      drawTopBar(p && p->name[0] ? p->name : (kids::toHex(nb, _thread_peer, 4), nb), true, false);
      drawBackHint();
      break;
    }
    case SCR_PEOPLE:
      drawPeople();
      drawTopBar("People", true, false, true);
      drawBackHint();
      break;
    case SCR_PIN:
      drawPinPad();
      drawTopBar("Parents", true, false);
      drawBackHint();
      break;
    case SCR_SETTINGS:
      drawSettings();
      drawTopBar("Settings", true, false);
      drawBackHint();
      break;
    case SCR_RENAME: {
      char cur[40];
      snprintf(cur, sizeof(cur), "Now: %s", _prefs.name);
      drawPrompt("Type your new name", cur, "Then OK. Your messages stay.");
      drawCompose("new name", false, H_OK, "OK");
      drawTopBar("Change name", true, false);
      drawBackHint();
      break;
    }
    case SCR_MENU:
      drawMenu();
      drawTopBar("Me", true, false);
      drawBackHint();
      break;
    case SCR_CONFIRM:
      drawConfirm();
      drawTopBar("Change name", true, false);
      drawBackHint();
      break;
    case SCR_TOUCHCAL:
      drawTouchCal();
      if (_cal_step >= 2) { drawTopBar("Touch test", true, false); drawBackHint(); }
      break;
  }
  hw.flush();
}

// =====================================================================================
// USB serial console (locked by the parent PIN)

void KidsMesh::serialLoop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (_serial_len > 0) {
        _serial_line[_serial_len] = 0;
        handleSerialCommand(_serial_line);
        _serial_len = 0;
      }
    } else if (_serial_len < (int)sizeof(_serial_line) - 1) {
      _serial_line[_serial_len++] = c;
    }
  }
}

void KidsMesh::handleSerialCommand(char* cmd) {
  char hex[65];
  if (strcmp(cmd, "info") == 0 || strcmp(cmd, "ver") == 0) {
    kids::toHex(hex, self_id.pub_key, 32);
    Serial.printf("SpArkKids %s\nme %s name '%s'\nlink %d people %d batt %umV clock %lu\n", KIDS_FW_VERSION, hex,
                  _prefs.name, (int)_link, _num_people, _batt_mv, (unsigned long)getRTCClock()->getCurrentTime());
    char diag[128];
    gps.formatDiagnostics(diag, sizeof(diag));
    Serial.printf("gps baud %d %s\n", _gps_baud, diag);
    Serial.printf("touch %s 0x%02X cal x %d..%d y %d..%d (%s)\n", hw.touchOk() ? "ok" : "none", hw.touchAddr(),
                  _cal.x0, _cal.x1, _cal.y0, _cal.y1, _prefs.cal_ok ? "saved" : "default");
    return;
  }
  if (strcmp(cmd, "touchraw on") == 0 || strcmp(cmd, "touchraw off") == 0) {
    _touchraw = cmd[10] == 'n';
    Serial.println("OK");
    return;
  }
  if (strncmp(cmd, "unlock ", 7) == 0) {
    _serial_unlocked = strcmp(cmd + 7, _prefs.pin) == 0;
    Serial.println(_serial_unlocked ? "OK" : "ERR");
    return;
  }
  if (!_serial_unlocked) { Serial.println("locked: unlock <pin> (info, touchraw on|off work without)"); return; }
  if (strncmp(cmd, "name ", 5) == 0) { applyName(cmd + 5); Serial.println("OK"); }
  else if (strncmp(cmd, "pw ", 3) == 0) { settingsSave(1, cmd + 3); Serial.println("OK"); }
  else if (strncmp(cmd, "key ", 4) == 0) { settingsSave(2, cmd + 4); kids::toHex(hex, _prefs.room_key, 32); Serial.printf("room %s\n", hex); }
  else if (strncmp(cmd, "pin ", 4) == 0) { settingsSave(3, cmd + 4); Serial.println("OK"); }
  else if (strcmp(cmd, "share on") == 0) { _prefs.share_loc = 1; savePrefs(); Serial.println("OK"); }
  else if (strcmp(cmd, "share off") == 0) { _prefs.share_loc = 0; savePrefs(); Serial.println("OK"); }
  else if (strcmp(cmd, "public on") == 0 || strcmp(cmd, "public off") == 0) {
    _prefs.public_mode = cmd[8] == 'n' ? kids::PUBLIC_ON : kids::PUBLIC_OFF;
    savePrefs();
    Serial.println("OK");
  } else if (strcmp(cmd, "public") == 0) {
    Serial.printf("public %s (%s)\n", publicOn() ? "on" : "off", _prefs.public_mode == kids::PUBLIC_DEFAULT ? "build default" : "set by parent");
  }
  else if (strcmp(cmd, "relogin") == 0) { settingsClick(8); Serial.println("OK"); }
  else if (strcmp(cmd, "advert") == 0) { sendAdvertNow(); Serial.println("OK"); }
  else if (strcmp(cmd, "members") == 0) {
    for (int i = 0; i < _num_people; i++) {
      kids::toHex(hex, _people[i].key, 4);
      Serial.printf("%s %s%s\n", hex, _people[i].name, (_people[i].flags & 1) ? " (parent)" : "");
    }
  } else if (strncmp(cmd, "touchcal ", 9) == 0) {
    int a, b, c, d;
    if (strcmp(cmd + 9, "default") == 0) { _prefs.cal_ok = 0; savePrefs(); applyCal(); Serial.println("OK"); }
    else if (sscanf(cmd + 9, "%d %d %d %d", &a, &b, &c, &d) == 4) {
      _prefs.cal[0] = a; _prefs.cal[1] = b; _prefs.cal[2] = c; _prefs.cal[3] = d; _prefs.cal_ok = 1;
      savePrefs(); applyCal(); Serial.println("OK");
    } else Serial.println("touchcal <raw x0> <raw x1> <raw y0> <raw y1> | default");
  } else if (strncmp(cmd, "time ", 5) == 0) {
    uint32_t t = strtoul(cmd + 5, NULL, 10);
    if (t > getRTCClock()->getCurrentTime()) getRTCClock()->setCurrentTime(t);
    Serial.println("OK");
  } else if (strcmp(cmd, "reboot") == 0) { ESP.restart(); }
  else Serial.println("commands: info, name, pw, key, pin, share on|off, public [on|off], relogin, advert, members, touchraw on|off, touchcal, time <epoch>, reboot");
}
