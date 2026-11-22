#pragma once

#include <Arduino.h>
#include <Mesh.h>
#include <SPIFFS.h>
#include <helpers/ArduinoHelpers.h>
#include <helpers/StaticPoolPacketManager.h>
#include <helpers/SimpleMeshTables.h>
#include <helpers/IdentityStore.h>
#include <helpers/BaseChatMesh.h>
#include <helpers/TransportKeyStore.h>
#include <target.h>

#include "KidsLogic.h"
#include "KidsInput.h"
#include "MemberDir.h"
#include "PublicChannel.h"

// Build settings come from the repository's .env through spark/pio_env.py (see .env.example).
// There are deliberately no defaults for the room key, room password and parent PIN.
#ifndef KIDS_ROOM_KEY
  #error "KIDS_ROOM_KEY (the SparkNode's public key, 64 hex digits) must be defined (.env)"
#endif
#ifndef KIDS_ROOM_PASSWORD
  #error "KIDS_ROOM_PASSWORD must be defined (.env)"
#endif
#ifndef KIDS_ADMIN_PIN
  #error "KIDS_ADMIN_PIN (the parent PIN, .env KIDS_PARENT_PIN) must be defined"
#endif
#ifndef KIDS_ROOM_NAME
  #define KIDS_ROOM_NAME "SparkNode"
#endif
#ifndef KIDS_SCOPE_NAME
  #define KIDS_SCOPE_NAME ""          // empty: unscoped floods
#endif
#ifndef KIDS_PATH_HASH_SIZE
  #define KIDS_PATH_HASH_SIZE 1
#endif
#ifndef KIDS_PUBLIC_ENABLED
  #define KIDS_PUBLIC_ENABLED 0       // Public channel off unless a parent turns it on
#endif
#ifndef KIDS_PUBLIC_PER_HOUR
  #define KIDS_PUBLIC_PER_HOUR 6
#endif
#ifndef KIDS_TZ
  #define KIDS_TZ "UTC0"            // POSIX TZ string (.env SPARK_TZ) for the clock and message times
#endif
#ifndef KIDS_FW_VERSION
  #define KIDS_FW_VERSION "SparkKids-dev"
#endif

#define LOGIN_REFRESH_MS (10UL * 60 * 1000)    // re-login (keeps the room pushing to us)
#define MEMBERS_REFRESH_MS (6UL * 3600 * 1000)
#define SCREEN_IDLE_MS 60000UL
#define WAKE_ON_MSG_MS 10000UL
#define ADVERT_INTERVAL_MS (6UL * 3600 * 1000)
#define ADVERT_BOOT_DELAY_MS 8000UL

#define MSG_CAP 200
#define PUB_CAP 60          // Public posts: own RAM ring, so Public chatter never pushes out family messages
#define MSG_TEXT_MAX 155
#define COMPOSE_MAX 140
#define MAX_PEOPLE 32
#define MAX_PENDING 8

#define MSG_OUT     0x01
#define MSG_ACKED   0x02
#define MSG_FAILED  0x04
#define MSG_UNREAD  0x08

struct Msg {
  uint32_t id;           // 0 = empty slot
  uint8_t peer[6];       // all zero = the room, else the DM partner's key prefix
  uint8_t author[4];
  uint32_t ts;           // sender clock (room posts: room clock)
  uint8_t flags;
  char text[MSG_TEXT_MAX + 1];
};

struct Person {
  uint8_t key[32];
  char name[16];
  uint8_t flags;         // spark::MEMBER_FLAG_ADMIN
  uint8_t seen;          // transient: seen in the current directory fetch
};

struct KidsPrefs {
  uint32_t magic;
  char name[16];
  char room_pw[16];
  uint8_t room_key[32];
  char pin[9];
  uint8_t volume;
  uint8_t share_loc;
  uint8_t rsvd0;
  uint32_t clock_floor;
  uint32_t room_sync_since;
  int16_t cal[4];         // touch calibration x0, x1, y0, y1 (raw), valid if cal_ok
  uint8_t cal_ok;
  uint8_t trackball_off;
  uint8_t public_mode;    // kids::PUBLIC_DEFAULT / PUBLIC_OFF / PUBLIC_ON
  uint8_t rsvd[5];
};

struct Pending {
  uint32_t msg_id;
  uint32_t ack;
  unsigned long deadline;
  uint8_t attempt;
};

enum Screen { SCR_NAME, SCR_ROOM, SCR_PEOPLE, SCR_THREAD, SCR_PIN, SCR_SETTINGS, SCR_RENAME, SCR_CONFIRM,
              SCR_TOUCHCAL, SCR_MENU, SCR_PUBLIC };

static const uint8_t ZERO6[6] = { 0, 0, 0, 0, 0, 0 };
static const uint8_t PUBLIC6[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };   // peer marker of Public posts

// touch targets registered while drawing
enum HitId : uint8_t { H_BACK = 1, H_PEOPLE, H_SEND, H_OK, H_CANCEL, H_SAVE, H_ROW, H_AUTHOR, H_FAILED, H_NEWPILL,
                       H_YES, H_NO, H_VOL_DOWN, H_VOL_UP, H_GEAR, H_MENU_NAME, H_MENU_PARENT, H_DIGIT, H_DEL };
enum MembersState : uint8_t { MEMBERS_UNKNOWN, MEMBERS_OK, MEMBERS_FAILED };
struct Hit { int16_t x, y, w, h; uint8_t id; int16_t arg; };
#define MAX_HITS 24
enum Link { LINK_OFFLINE, LINK_LOGIN, LINK_ONLINE };

class KidsMesh : public BaseChatMesh {
public:
  KidsMesh(mesh::Radio& radio, StdRNG& rng, mesh::RTCClock& rtc, SimpleMeshTables& tables);
  void begin(FILESYSTEM& fs);
  void loop();
  void serialLoop();

protected:
  float getAirtimeBudgetFactor() const override { return 1.0f; }
  int calcRxDelay(float score, uint32_t air_time) const override { return 0; }
  bool allowPacketForward(const mesh::Packet* packet) override { return false; }   // never repeat
  bool isAutoAddEnabled() const override { return false; }
  bool shouldAutoAddContactType(uint8_t type) const override { return false; }
  void onDiscoveredContact(ContactInfo& contact, bool is_new, uint8_t path_len, const uint8_t* path) override;
  ContactInfo* processAck(const uint8_t* data) override;
  void onContactPathUpdated(const ContactInfo& contact) override {}
  void onMessageRecv(const ContactInfo& from, mesh::Packet* pkt, uint32_t sender_timestamp, const char* text) override;
  void onCommandDataRecv(const ContactInfo&, mesh::Packet*, uint32_t, const char*) override {}
  void onCLICommandRecv(const ContactInfo&, mesh::Packet*, uint32_t, const char*, char* reply) override { reply[0] = 0; }
  void onSignedMessageRecv(const ContactInfo& from, mesh::Packet* pkt, uint32_t sender_timestamp, const uint8_t* sender_prefix, const char* text) override;
  uint32_t calcFloodTimeoutMillisFor(uint32_t pkt_airtime_millis) const override;
  uint32_t calcDirectTimeoutMillisFor(uint32_t pkt_airtime_millis, uint8_t path_len) const override;
  void onSendTimeout() override {}
  void onChannelMessageRecv(const mesh::GroupChannel& channel, mesh::Packet* pkt, uint32_t timestamp, const char* text) override;
  int searchChannelsByHash(const uint8_t* hash, mesh::GroupChannel channels[], int max_matches) override;
  uint8_t onContactRequest(const ContactInfo&, uint32_t, const uint8_t*, uint8_t, uint8_t*) override { return 0; }
  void onContactResponse(const ContactInfo& contact, const uint8_t* data, uint8_t len) override;
  void sendFloodScoped(const ContactInfo& recipient, mesh::Packet* pkt, uint32_t delay_millis = 0) override;
  void sendFloodScoped(const mesh::GroupChannel& channel, mesh::Packet* pkt, uint32_t delay_millis = 0) override;

private:
  FILESYSTEM* _fs;
  KidsPrefs _prefs;
  TransportKey _scope;
  kids::TzRule _tz;           // local time (KIDS_TZ)
  bool _scoped;               // KIDS_SCOPE_NAME set: floods carry its transport code
  Msg* _msgs;                 // PSRAM ring
  int _msg_head;              // next slot to write
  Msg* _pub;                  // Public ring (RAM only, not persisted)
  int _pub_head;
  mesh::GroupChannel _public;
  kids::PublicRate _pub_rate;
  const char* _notice;        // short banner above the compose line
  char _notice_buf[48];
  unsigned long _notice_until;
  uint32_t _next_msg_id;
  Person _people[MAX_PEOPLE];
  int _num_people;
  Pending _pending[MAX_PENDING];

  // link
  Link _link;
  unsigned long _next_login, _login_deadline;
  int _login_fails;
  uint32_t _last_room_heard;     // uptime secs
  uint32_t _members_tag; unsigned long _members_deadline; uint8_t _members_next; unsigned long _next_members;
  MembersState _members_state; uint8_t _members_fails;
  uint32_t _loc_tag; kids::LocState _loc; int32_t _loc_lat, _loc_lon;
  unsigned long _next_gps_check, _gps_baud_switch; uint32_t _gps_ok_prev; bool _gps_seen; int _gps_baud;

  // ui
  Screen _screen;
  bool _dirty;
  unsigned long _last_draw, _last_input, _wake_until, _next_advert;
  char _compose[COMPOSE_MAX + 1];       // PIN, name, settings edits
  char _draft_room[COMPOSE_MAX + 1];    // kept per screen: a back swipe never loses text
  char _draft_dm[COMPOSE_MAX + 1];
  char _draft_pub[COMPOSE_MAX + 1];
  uint8_t _draft_dm_peer[6];
  char _new_name[16];
  uint8_t _thread_peer[6];
  Screen _back_to;            // where back goes from a DM
  Screen _pin_back;           // where back goes from the PIN screen / settings
  bool _pin_wrong;
  int _scroll_px;             // chat: pixels scrolled up from the newest message
  int _list_px;               // lists: pixels scrolled down from the top
  int _sel;                   // keyboard/trackball focus row (lists)
  bool _sel_visible;
  int _edit;                  // settings row being edited, -1 none
  bool _admin;                // settings unlocked
  uint16_t _batt_mv;
  unsigned long _next_batt, _next_floor_save, _msgs_save_at, _people_save_at;
  unsigned long _empty_since; // compose line empty since (backspace-as-back guard)
  char _serial_line[200];
  int _serial_len;
  bool _serial_unlocked;
  // touch
  kids::Gesture _gest;
  kids::TouchCal _cal;
  Hit _hits[MAX_HITS];
  int _nhits;
  int _pressed;               // index into _hits while a finger is on it, -1 none
  uint8_t _back_hint;         // 0..100 swipe-back progress
  bool _touchraw;
  int _cal_step;              // calibration: 0, 1 = crosses, 2 = free test
  long _cal_sx, _cal_sy; int _cal_n; unsigned long _cal_down_at;
  int16_t _cal_raw[4];
  int16_t _cal_dot_x, _cal_dot_y;
  const char* _cal_msg;

  // persistence
  void loadPrefs(); void savePrefs();
  void loadPeople(); void savePeople();
  void loadMsgs(); void saveMsgs();
  void setupRoomContact();

  // messages
  ContactInfo* room();
  bool isRoomKey(const uint8_t* key) const { return memcmp(key, _prefs.room_key, 32) == 0; }
  Msg* addMsg(const uint8_t* peer6, const uint8_t* author4, uint32_t ts, uint8_t flags, const char* text);
  Msg* findMsg(uint32_t id);
  bool isRoomPeer(const uint8_t* peer6) const;
  bool isPublicPeer(const uint8_t* peer6) const { return memcmp(peer6, PUBLIC6, 6) == 0; }
  bool publicOn() const { return kids::publicEnabled(_prefs.public_mode, KIDS_PUBLIC_ENABLED); }
  void sendPublic(const char* text);
  void openPublic(Screen back_to);
  void setNotice(const char* text, unsigned long ms);
  void drawNotice();
  bool chatScreen() const { return _screen == SCR_ROOM || _screen == SCR_THREAD || _screen == SCR_PUBLIC; }
  int unreadFor(const uint8_t* peer6) const;
  int unreadDMs() const;
  void markRead(const uint8_t* peer6);
  void sendText(const uint8_t* peer6, const char* text);
  bool transmit(Msg& m, uint8_t attempt);
  void checkPending();
  void resendFailed(const uint8_t* peer6);
  const char* authorName(const uint8_t* prefix4, char* buf);
  Person* findPerson(const uint8_t* key, int len);

  // link
  void linkLoop();
  void sendLoginNow();
  void requestMembers(uint8_t start);
  void locationLoop();
  void gpsLoop();
  uint32_t upSecs() const { return millis() / 1000; }

  // ui
  void sendAdvertNow();
  void applyName(const char* name);
  void applyCal();
  void uiLoop();
  void touchLoop();
  void calSample(uint8_t points, int16_t rx, int16_t ry);
  void onEvent(const kids::UiEvent& e);
  void onKey(int c);
  void onTrack(int ev);
  void onTap(const Hit& h);
  void goBack();
  void go(Screen s);
  char* composeBuf();
  int composeMax() const;
  void submitCompose();
  void openThread(const uint8_t* peer6, Screen back_to);
  void openRow(int row);
  void scrollBy(int dy);
  void addHit(int x, int y, int w, int h, uint8_t id, int arg = 0);
  int hitTest(int x, int y) const;
  bool isPressed(uint8_t id, int arg = 0) const;
  void draw();
  void drawTopBar(const char* title, bool back, bool people_btn, bool gear = false);
  void drawGear(int cx, int cy, uint16_t col);
  void drawMenu();
  void drawPinPad();
  void pinKey(char c);
  void openPin(Screen back);
  void drawButton(int x, int y, int w, int h, const char* label, uint8_t id, bool enabled, uint16_t fill);
  void drawThread(const uint8_t* peer6);
  void drawCompose(const char* prompt, bool mask, uint8_t action_id, const char* action_label);
  void drawBackHint();
  void drawPeople();
  void drawSettings();
  void drawPrompt(const char* line1, const char* line2, const char* hint);
  void drawConfirm();
  void drawTouchCal();
  int settingsRows() const { return 14; }
  void settingsRow(int i, char* label, char* value);
  void settingsClick(int i);
  void settingsSave(int i, const char* v);
  void wake(unsigned long ms);
  void handleSerialCommand(char* cmd);
  int pubRows() const { return publicOn() ? 1 : 0; }
  int peopleRows() const { return 1 + pubRows() + _num_people; }   // room, [Public], people
};
