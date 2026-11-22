#include "KidsApp.h"
#include "Hw.h"

#define PREFS_MAGIC 0x4B494453   // "KIDS"
#define PREFS_FILE "/kprefs"
#define PEOPLE_FILE "/kpeople"
#define MSGS_FILE "/kmsgs"



static File openWrite(FILESYSTEM* fs, const char* name) { return fs->open(name, "w", true); }

// =====================================================================================
// setup / persistence

KidsMesh::KidsMesh(mesh::Radio& radio, StdRNG& rng, mesh::RTCClock& rtc, SimpleMeshTables& tables)
    : BaseChatMesh(radio, *new ArduinoMillis(), rng, rtc, *new StaticPoolPacketManager(16), tables) {
  _fs = NULL;
  _msgs = NULL;
  _msg_head = 0;
  _pub = NULL;
  _pub_head = 0;
  _scoped = false;
  kids::parseTz(KIDS_TZ, _tz);   // invalid: UTC
  memset(&_public, 0, sizeof(_public));
  memset(&_pub_rate, 0, sizeof(_pub_rate));
  _notice = NULL;
  _notice_buf[0] = 0;
  _notice_until = 0;
  _next_msg_id = 1;
  _num_people = 0;
  memset(_pending, 0, sizeof(_pending));
  _link = LINK_OFFLINE;
  _next_login = 3000;
  _login_deadline = 0;
  _login_fails = 0;
  _last_room_heard = 0;
  _members_tag = 0; _members_deadline = 0; _members_next = 0; _next_members = 0;
  _members_state = MEMBERS_UNKNOWN; _members_fails = 0;
  _pin_back = SCR_ROOM; _pin_wrong = false;
  _loc_tag = 0; memset(&_loc, 0, sizeof(_loc)); _loc_lat = _loc_lon = 0;
  _next_gps_check = 0; _gps_baud_switch = 0; _gps_ok_prev = 0; _gps_seen = false; _gps_baud = 38400;
  _screen = SCR_ROOM;
  _dirty = true;
  _last_draw = _last_input = _wake_until = 0;
  _compose[0] = 0;
  memset(_thread_peer, 0, 6);
  _scroll_px = _list_px = 0;
  _sel = 0;
  _sel_visible = false;
  _pressed = -1;
  _back_hint = 0;
  _back_to = SCR_PEOPLE;
  _draft_room[0] = _draft_dm[0] = _draft_pub[0] = 0;
  _new_name[0] = 0;
  _empty_since = 0;
  _nhits = 0;
  _touchraw = false;
  _cal_step = 0; _cal_n = 0; _cal_sx = _cal_sy = 0; _cal_down_at = 0;
  _cal_dot_x = _cal_dot_y = -1;
  _next_advert = 0;
  _cal_msg = NULL;
  _edit = -1;
  _admin = false;
  _batt_mv = 0;
  _next_batt = _next_floor_save = _msgs_save_at = _people_save_at = 0;
  _serial_len = 0;
  _serial_unlocked = false;
}

void KidsMesh::begin(FILESYSTEM& fs) {
  _fs = &fs;
  BaseChatMesh::begin();

  IdentityStore store(fs, "/identity");
  store.begin();
  if (!store.load("_main", self_id)) {
    self_id = radio_new_identity();
    int count = 0;
    while (count < 10 && (self_id.pub_key[0] == 0x00 || self_id.pub_key[0] == 0xFF)) {
      self_id = radio_new_identity(); count++;
    }
    store.save("_main", self_id);
  }

  _scoped = KIDS_SCOPE_NAME[0] != 0;
  if (_scoped) {
    TransportKeyStore temp;
    temp.getAutoKeyFor(0, "#" KIDS_SCOPE_NAME, _scope);
  }
  spark::initPublicChannel(_public, [](uint8_t* h, size_t hl, const uint8_t* m, int n) { mesh::Utils::sha256(h, hl, m, n); });

  loadPrefs();
  // clock: never start below the persisted floor (the room rejects older login timestamps)
  uint32_t now = getRTCClock()->getCurrentTime();
  uint32_t boot = kids::bootClock(now, _prefs.clock_floor);
  if (boot != now) getRTCClock()->setCurrentTime(boot);

  _msgs = (Msg*)ps_calloc(MSG_CAP, sizeof(Msg));
  if (!_msgs) _msgs = (Msg*)calloc(MSG_CAP, sizeof(Msg));
  loadMsgs();
  _pub = (Msg*)ps_calloc(PUB_CAP, sizeof(Msg));
  if (!_pub) _pub = (Msg*)calloc(PUB_CAP, sizeof(Msg));
  setupRoomContact();
  loadPeople();

  hw.setVolume(_prefs.volume);
  applyCal();
  if (_prefs.name[0]) _next_advert = millis() + ADVERT_BOOT_DELAY_MS;
  _screen = _prefs.name[0] ? SCR_ROOM : SCR_NAME;
  _last_input = millis();

  // GPS (T-Deck Plus: u-blox at 38400 or L76K at 9600, auto-detected in gpsLoop)
  Serial1.setPins(PIN_GPS_TX, PIN_GPS_RX);
  Serial1.begin(_gps_baud);
  gps.begin();
  _gps_baud_switch = millis() + 4000;
}

void KidsMesh::loadPrefs() {
  memset(&_prefs, 0, sizeof(_prefs));
  bool ok = false;
  if (_fs->exists(PREFS_FILE)) {
    File f = _fs->open(PREFS_FILE);
    if (f) {
      ok = f.read((uint8_t*)&_prefs, sizeof(_prefs)) == sizeof(_prefs) && _prefs.magic == PREFS_MAGIC;
      f.close();
    }
  }
  if (!ok) {
    memset(&_prefs, 0, sizeof(_prefs));
    _prefs.magic = PREFS_MAGIC;
    StrHelper::strncpy(_prefs.room_pw, KIDS_ROOM_PASSWORD, sizeof(_prefs.room_pw));
    kids::parseKey(KIDS_ROOM_KEY, _prefs.room_key);
    StrHelper::strncpy(_prefs.pin, KIDS_ADMIN_PIN, sizeof(_prefs.pin));
    _prefs.volume = 2;
    _prefs.share_loc = 1;
    savePrefs();
  }
}

void KidsMesh::savePrefs() {
  File f = openWrite(_fs, PREFS_FILE);
  if (f) { f.write((const uint8_t*)&_prefs, sizeof(_prefs)); f.close(); }
}

void KidsMesh::loadPeople() {
  _num_people = 0;
  if (!_fs->exists(PEOPLE_FILE)) return;
  File f = _fs->open(PEOPLE_FILE);
  if (!f) return;
  uint8_t n = 0;
  if (f.read(&n, 1) == 1) {
    for (int i = 0; i < n && i < MAX_PEOPLE; i++) {
      Person p;
      memset(&p, 0, sizeof(p));
      if (f.read(p.key, 32) != 32 || f.read((uint8_t*)p.name, 16) != 16 || f.read(&p.flags, 1) != 1) break;
      p.name[15] = 0;
      if (isRoomKey(p.key) || memcmp(p.key, self_id.pub_key, 32) == 0) continue;
      _people[_num_people++] = p;
      ContactInfo c;
      memset(&c, 0, sizeof(c));
      c.id = mesh::Identity(p.key);
      StrHelper::strncpy(c.name, p.name, sizeof(c.name));
      c.type = ADV_TYPE_CHAT;
      c.out_path_len = OUT_PATH_UNKNOWN;
      addContact(c);
    }
  }
  f.close();
}

void KidsMesh::savePeople() {
  File f = openWrite(_fs, PEOPLE_FILE);
  if (!f) return;
  uint8_t n = _num_people;
  f.write(&n, 1);
  for (int i = 0; i < _num_people; i++) {
    f.write(_people[i].key, 32);
    f.write((const uint8_t*)_people[i].name, 16);
    f.write(&_people[i].flags, 1);
  }
  f.close();
}

void KidsMesh::loadMsgs() {
  if (!_msgs || !_fs->exists(MSGS_FILE)) return;
  File f = _fs->open(MSGS_FILE);
  if (!f) return;
  uint32_t hdr[3];
  if (f.read((uint8_t*)hdr, sizeof(hdr)) == sizeof(hdr) && hdr[0] == 0x4B4D5331 && hdr[1] == sizeof(Msg)) {
    _msg_head = hdr[2] % MSG_CAP;
    f.read((uint8_t*)_msgs, MSG_CAP * sizeof(Msg));
    for (int i = 0; i < MSG_CAP; i++) {
      if (_msgs[i].id >= _next_msg_id) _next_msg_id = _msgs[i].id + 1;
      _msgs[i].text[MSG_TEXT_MAX] = 0;
    }
  }
  f.close();
}

void KidsMesh::saveMsgs() {
  if (!_msgs) return;
  File f = openWrite(_fs, MSGS_FILE);
  if (!f) return;
  uint32_t hdr[3] = { 0x4B4D5331, sizeof(Msg), (uint32_t)_msg_head };
  f.write((const uint8_t*)hdr, sizeof(hdr));
  f.write((const uint8_t*)_msgs, MSG_CAP * sizeof(Msg));
  f.close();
}

void KidsMesh::setupRoomContact() {
  ContactInfo c;
  memset(&c, 0, sizeof(c));
  c.id = mesh::Identity(_prefs.room_key);
  StrHelper::strncpy(c.name, KIDS_ROOM_NAME, sizeof(c.name));
  c.type = ADV_TYPE_ROOM;
  c.out_path_len = OUT_PATH_UNKNOWN;
  c.sync_since = _prefs.room_sync_since;
  addContact(c);
}

ContactInfo* KidsMesh::room() { return lookupContactByPubKey(_prefs.room_key, 32); }

// =====================================================================================
// mesh callbacks

void KidsMesh::sendFloodScoped(const ContactInfo& recipient, mesh::Packet* pkt, uint32_t delay_millis) {
  if (!_scoped) { sendFlood(pkt, delay_millis, KIDS_PATH_HASH_SIZE); return; }
  uint16_t codes[2] = { _scope.calcTransportCode(pkt), 0 };
  sendFlood(pkt, codes, delay_millis, KIDS_PATH_HASH_SIZE);
}
// Room traffic and Public posts alike: same scope, same path hash size.
void KidsMesh::sendFloodScoped(const mesh::GroupChannel& channel, mesh::Packet* pkt, uint32_t delay_millis) {
  if (!_scoped) { sendFlood(pkt, delay_millis, KIDS_PATH_HASH_SIZE); return; }
  uint16_t codes[2] = { _scope.calcTransportCode(pkt), 0 };
  sendFlood(pkt, codes, delay_millis, KIDS_PATH_HASH_SIZE);
}

// Public channel: only decoded while enabled, so a device with Public off neither shows it
// nor spends time on it.
int KidsMesh::searchChannelsByHash(const uint8_t* hash, mesh::GroupChannel channels[], int max_matches) {
  if (!publicOn() || max_matches < 1 || !spark::channelHashMatches(_public, hash)) return 0;
  channels[0] = _public;
  return 1;
}

void KidsMesh::onChannelMessageRecv(const mesh::GroupChannel& channel, mesh::Packet* pkt, uint32_t timestamp, const char* text) {
  if (!publicOn() || memcmp(channel.secret, _public.secret, sizeof(_public.secret)) != 0) return;
  char name[16];
  kids::splitChannelText(text, name, sizeof(name));
  uint8_t tag[4];
  kids::nameTag(name, tag);
  bool viewing = _screen == SCR_PUBLIC && hw.isBacklightOn() && _scroll_px < 8;
  addMsg(PUBLIC6, tag, timestamp, viewing ? 0 : MSG_UNREAD, text);   // quiet: no sound, no wake
}

void KidsMesh::sendPublic(const char* text) {
  if (!publicOn()) return;
  uint32_t now = upSecs() | 1, wait = 0;
  if (!kids::publicRateAllow(_pub_rate, now, KIDS_PUBLIC_PER_HOUR, &wait)) {
    if (wait >= 120) snprintf(_notice_buf, sizeof(_notice_buf), "Public: wait %lu min", (unsigned long)((wait + 59) / 60));
    else snprintf(_notice_buf, sizeof(_notice_buf), "Public: wait %lu s", (unsigned long)wait);
    setNotice(_notice_buf, 4000);
    hw.beepFail();
    return;
  }
  uint32_t ts = getRTCClock()->getCurrentTimeUnique();
  Msg* m = addMsg(PUBLIC6, self_id.pub_key, ts, MSG_OUT, text);
  bool ok = sendGroupMessage(ts, _public, _prefs.name[0] ? _prefs.name : "kid", text, strlen(text));
  if (ok) publicRateRecord(_pub_rate, now);
  if (m) m->flags |= ok ? MSG_ACKED : MSG_FAILED;   // no acks on channels: "acked" = went out
}

void KidsMesh::setNotice(const char* text, unsigned long ms) {
  _notice = text;
  _notice_until = millis() + ms;
  _dirty = true;
}

uint32_t KidsMesh::calcFloodTimeoutMillisFor(uint32_t pkt_airtime_millis) const {
  return 500 + 16.0f * pkt_airtime_millis;
}
uint32_t KidsMesh::calcDirectTimeoutMillisFor(uint32_t pkt_airtime_millis, uint8_t path_len) const {
  uint8_t n = path_len & 63;
  return 500 + ((pkt_airtime_millis * 6.0f + 250) * (n + 1));
}

void KidsMesh::onDiscoveredContact(ContactInfo& contact, bool is_new, uint8_t path_len, const uint8_t* path) {
  // Only adverts of known people get here as existing contacts; strangers are never added.
  if (is_new) return;
  Person* p = findPerson(contact.id.pub_key, 32);
  if (p && contact.name[0] && strcmp(p->name, contact.name) != 0) {
    char tmp[16];
    spark::memberCopyName(tmp, contact.name, strlen(contact.name));
    if (tmp[0]) {
      strcpy(p->name, tmp);
      _people_save_at = millis() + 5000;
      _dirty = true;
    }
  }
}

ContactInfo* KidsMesh::processAck(const uint8_t* data) {
  for (int i = 0; i < MAX_PENDING; i++) {
    Pending& p = _pending[i];
    if (p.msg_id && memcmp(data, &p.ack, 4) == 0) {
      Msg* m = findMsg(p.msg_id);
      p.msg_id = 0;
      if (!m) return NULL;
      m->flags = (m->flags | MSG_ACKED) & ~MSG_FAILED;
      _msgs_save_at = millis() + 20000;
      _dirty = true;
      if (isRoomPeer(m->peer)) { _last_room_heard = upSecs(); return room(); }
      return lookupContactByPubKey(m->peer, 6);
    }
  }
  return NULL;
}

void KidsMesh::onMessageRecv(const ContactInfo& from, mesh::Packet* pkt, uint32_t sender_timestamp, const char* text) {
  if (isRoomKey(from.id.pub_key)) return;            // the room only sends signed posts to us
  if (!findPerson(from.id.pub_key, 32)) return;      // not a room member
  uint8_t peer[6];
  memcpy(peer, from.id.pub_key, 6);
  bool viewing = _screen == SCR_THREAD && memcmp(_thread_peer, peer, 6) == 0 && hw.isBacklightOn() && _scroll_px < 8;
  Msg* m = addMsg(peer, from.id.pub_key, sender_timestamp, viewing ? 0 : MSG_UNREAD, text);
  if (m) { hw.beepDM(); wake(WAKE_ON_MSG_MS); }
}

void KidsMesh::onSignedMessageRecv(const ContactInfo& from, mesh::Packet* pkt, uint32_t sender_timestamp,
                                   const uint8_t* sender_prefix, const char* text) {
  if (!isRoomKey(from.id.pub_key)) return;
  _last_room_heard = upSecs();
  if (_link != LINK_ONLINE) { _link = LINK_ONLINE; _dirty = true; }
  if (sender_timestamp > _prefs.room_sync_since) {
    _prefs.room_sync_since = sender_timestamp;
    ContactInfo* r = room();
    if (r) r->sync_since = sender_timestamp;
  }
  if (memcmp(sender_prefix, self_id.pub_key, 4) == 0) return;   // our own post (not normally pushed)
  bool viewing = _screen == SCR_ROOM && hw.isBacklightOn() && _scroll_px < 8;
  Msg* m = addMsg(ZERO6, sender_prefix, sender_timestamp, viewing ? 0 : MSG_UNREAD, text);
  if (m) { hw.beepRoom(); wake(WAKE_ON_MSG_MS); }
}

void KidsMesh::onContactResponse(const ContactInfo& contact, const uint8_t* data, uint8_t len) {
  if (!isRoomKey(contact.id.pub_key) || len < 5) return;
  _last_room_heard = upSecs();
  uint32_t tag;
  memcpy(&tag, data, 4);

  if (_members_tag && tag == _members_tag && data[4] == spark::REQ_GET_MEMBERS) {
    _members_tag = 0;
    uint8_t total = 0, start = 0;
    static spark::MemberPageItem items[8];
    int n = spark::memberPageDecode(&data[4], len - 4, total, start, items, 8);
    Serial.printf("members: page start %u n %d total %u\n", start, n, total);
    if (n < 0) return;
    if (start == 0) for (int i = 0; i < _num_people; i++) _people[i].seen = 0;
    for (int i = 0; i < n; i++) {
      const uint8_t* key = items[i].key;
      if (isRoomKey(key) || memcmp(key, self_id.pub_key, 32) == 0) continue;
      Person* p = findPerson(key, 32);
      if (!p && _num_people < MAX_PEOPLE) {
        p = &_people[_num_people++];
        memset(p, 0, sizeof(*p));
        memcpy(p->key, key, 32);
      }
      if (!p) continue;
      p->seen = 1;
      p->flags = items[i].flags;
      if (items[i].name[0]) StrHelper::strncpy(p->name, items[i].name, sizeof(p->name));
      ContactInfo* c = lookupContactByPubKey(key, 32);
      if (!c) {
        ContactInfo ci;
        memset(&ci, 0, sizeof(ci));
        ci.id = mesh::Identity(key);
        StrHelper::strncpy(ci.name, p->name, sizeof(ci.name));
        ci.type = ADV_TYPE_CHAT;
        ci.out_path_len = OUT_PATH_UNKNOWN;
        addContact(ci);
      } else {
        StrHelper::strncpy(c->name, p->name, sizeof(c->name));
      }
    }
    int next = start + n;
    if (n > 0 && next < total) {
      _members_next = next;
      _next_members = millis() + 1500;
    } else {
      // complete: drop people the room no longer lists
      for (int i = 0; i < _num_people;) {
        if (!_people[i].seen) {
          ContactInfo* c = lookupContactByPubKey(_people[i].key, 32);
          if (c) removeContact(*c);
          _people[i] = _people[_num_people - 1];
          _num_people--;
        } else i++;
      }
      // parents (admins) first, then by name
      for (int i = 1; i < _num_people; i++) {
        Person t = _people[i];
        int j = i - 1;
        while (j >= 0) {
          bool ta = t.flags & spark::MEMBER_FLAG_ADMIN, ja = _people[j].flags & spark::MEMBER_FLAG_ADMIN;
          bool before = (ta && !ja) || (ta == ja && strcasecmp(t.name, _people[j].name) < 0);
          if (!before) break;
          _people[j + 1] = _people[j];
          j--;
        }
        _people[j + 1] = t;
      }
      _members_next = 0;
      _next_members = millis() + MEMBERS_REFRESH_MS;
      _members_state = MEMBERS_OK;
      _members_fails = 0;
      savePeople();
    }
    _dirty = true;
    return;
  }

  if (_loc_tag && tag == _loc_tag && data[4] == spark::REQ_SET_LOCATION) {
    _loc_tag = 0;
    if (len >= 6 && data[5] == 0) {
      _loc.sent = true;
      _loc.sent_at = upSecs();
      _loc.lat = _loc_lat;
      _loc.lon = _loc_lon;
      _loc.tried_at = 0;
    }
    return;
  }

  if (_link == LINK_LOGIN && data[4] == RESP_SERVER_LOGIN_OK) {
    // login OK: data[0..3] is the room's clock
    uint32_t now = getRTCClock()->getCurrentTime();
    if (kids::acceptRoomTime(now, tag, gps.isValid() && !gps.waitingTimeSync())) {
      getRTCClock()->setCurrentTime(tag);
    }
    _link = LINK_ONLINE;
    _login_fails = 0;
    _next_login = millis() + LOGIN_REFRESH_MS;
    if (_members_next == 0 && (long)(millis() - _next_members) >= 0) _next_members = millis() + 2000;
    _dirty = true;
  }
}

// =====================================================================================
// messages

bool KidsMesh::isRoomPeer(const uint8_t* peer6) const { return memcmp(peer6, ZERO6, 6) == 0; }

Msg* KidsMesh::addMsg(const uint8_t* peer6, const uint8_t* author4, uint32_t ts, uint8_t flags, const char* text) {
  bool pub = isPublicPeer(peer6);
  Msg* ring = pub ? _pub : _msgs;
  int cap = pub ? PUB_CAP : MSG_CAP;
  int& head = pub ? _pub_head : _msg_head;
  if (!ring) return NULL;
  if (!(flags & MSG_OUT)) {   // drop duplicates (re-pushed posts, DM retries)
    for (int k = 1; k <= 60 && k <= cap; k++) {
      Msg& o = ring[(head - k + cap) % cap];
      if (o.id && o.ts == ts && memcmp(o.peer, peer6, 6) == 0 && memcmp(o.author, author4, 4) == 0) return NULL;
    }
  }
  Msg& m = ring[head];
  head = (head + 1) % cap;
  memset(&m, 0, sizeof(m));
  m.id = _next_msg_id++;
  memcpy(m.peer, peer6, 6);
  memcpy(m.author, author4, 4);
  m.ts = ts;
  m.flags = flags;
  StrHelper::strncpy(m.text, text, sizeof(m.text));
  if (!pub) _msgs_save_at = millis() + 20000;
  _dirty = true;
  return &m;
}

Msg* KidsMesh::findMsg(uint32_t id) {
  if (!_msgs || !id) return NULL;
  for (int i = 0; i < MSG_CAP; i++) if (_msgs[i].id == id) return &_msgs[i];
  return NULL;
}

int KidsMesh::unreadFor(const uint8_t* peer6) const {
  int n = 0;
  bool pub = isPublicPeer(peer6);
  const Msg* ring = pub ? _pub : _msgs;
  int cap = pub ? PUB_CAP : MSG_CAP;
  if (!ring) return 0;
  for (int i = 0; i < cap; i++)
    if (ring[i].id && (ring[i].flags & MSG_UNREAD) && memcmp(ring[i].peer, peer6, 6) == 0) n++;
  return n;
}
int KidsMesh::unreadDMs() const {
  int n = 0;
  if (!_msgs) return 0;
  for (int i = 0; i < MSG_CAP; i++)
    if (_msgs[i].id && (_msgs[i].flags & MSG_UNREAD) && !isRoomPeer(_msgs[i].peer)) n++;
  return n;
}
void KidsMesh::markRead(const uint8_t* peer6) {
  bool pub = isPublicPeer(peer6);
  Msg* ring = pub ? _pub : _msgs;
  int cap = pub ? PUB_CAP : MSG_CAP;
  if (!ring) return;
  bool any = false;
  for (int i = 0; i < cap; i++) {
    if (ring[i].id && (ring[i].flags & MSG_UNREAD) && memcmp(ring[i].peer, peer6, 6) == 0) {
      ring[i].flags &= ~MSG_UNREAD;
      any = true;
    }
  }
  if (any) { if (!pub) _msgs_save_at = millis() + 20000; _dirty = true; }
}

Person* KidsMesh::findPerson(const uint8_t* key, int len) {
  for (int i = 0; i < _num_people; i++) if (memcmp(_people[i].key, key, len) == 0) return &_people[i];
  return NULL;
}

const char* KidsMesh::authorName(const uint8_t* prefix4, char* buf) {
  if (memcmp(prefix4, self_id.pub_key, 4) == 0) return "Me";
  if (memcmp(prefix4, _prefs.room_key, 4) == 0) return KIDS_ROOM_NAME;
  Person* p = findPerson(prefix4, 4);
  if (p && p->name[0]) return p->name;
  kids::toHex(buf, prefix4, 4);
  return buf;
}

void KidsMesh::sendText(const uint8_t* peer6, const char* text) {
  uint32_t ts = getRTCClock()->getCurrentTimeUnique();
  Msg* m = addMsg(peer6, self_id.pub_key, ts, MSG_OUT, text);
  if (!m) return;
  if (!transmit(*m, 0)) m->flags |= MSG_FAILED;
}

bool KidsMesh::transmit(Msg& m, uint8_t attempt) {
  ContactInfo* c = isRoomPeer(m.peer) ? room() : lookupContactByPubKey(m.peer, 6);
  if (!c) return false;
  if (attempt >= 2 && c->out_path_len != OUT_PATH_UNKNOWN) resetPathTo(*c);   // last try: flood
  uint32_t ack = 0, est = 0;
  int r = sendMessage(*c, m.ts, attempt, m.text, ack, est);
  if (r == MSG_SEND_FAILED) return false;
  int slot = 0;
  for (int i = 0; i < MAX_PENDING; i++) {
    if (_pending[i].msg_id == 0) { slot = i; break; }
    if (_pending[i].deadline < _pending[slot].deadline) slot = i;
  }
  _pending[slot].msg_id = m.id;
  _pending[slot].ack = ack;
  _pending[slot].deadline = millis() + est + 3000;
  _pending[slot].attempt = attempt;
  m.flags &= ~MSG_FAILED;
  return true;
}

void KidsMesh::checkPending() {
  for (int i = 0; i < MAX_PENDING; i++) {
    Pending& p = _pending[i];
    if (!p.msg_id || (long)(millis() - p.deadline) < 0) continue;
    Msg* m = findMsg(p.msg_id);
    uint8_t attempt = p.attempt;
    p.msg_id = 0;
    if (!m) continue;
    if (attempt < 2 && transmit(*m, attempt + 1)) continue;
    m->flags |= MSG_FAILED;
    _dirty = true;
  }
}

void KidsMesh::resendFailed(const uint8_t* peer6) {
  if (!_msgs) return;
  for (int i = 0; i < MSG_CAP; i++) {
    Msg& m = _msgs[i];
    if (m.id && (m.flags & MSG_FAILED) && memcmp(m.peer, peer6, 6) == 0) {
      if (transmit(m, 0)) _dirty = true;
    }
  }
}

// =====================================================================================
// link: login, members, location, GPS

void KidsMesh::sendLoginNow() {
  ContactInfo* r = room();
  if (!r) return;
  r->sync_since = _prefs.room_sync_since;
  uint32_t est = 0;
  int res = sendLogin(*r, _prefs.room_pw, est);
  _link = res == MSG_SEND_FAILED ? LINK_OFFLINE : LINK_LOGIN;
  uint32_t t = est < 3000 ? 3000 : est;
  _login_deadline = millis() + 2 * t + 5000;
  _dirty = true;
}

void KidsMesh::requestMembers(uint8_t start) {
  ContactInfo* r = room();
  if (!r) return;
  uint8_t buf[24];
  size_t len = spark::memberReqEncode(buf, start, _prefs.name);
  uint32_t tag = 0, est = 0;
  if (sendRequest(*r, buf, len, tag, est) == MSG_SEND_FAILED) return;
  _members_tag = tag;
  uint32_t t = est < 3000 ? 3000 : est;
  _members_deadline = millis() + 2 * t + 5000;
}

void KidsMesh::linkLoop() {
  unsigned long now = millis();
  if (_link == LINK_LOGIN && (long)(now - _login_deadline) >= 0) {
    _login_fails++;
    ContactInfo* r = room();
    if (r && _login_fails >= 2) resetPathTo(*r);   // path may be stale: flood next time
    _link = LINK_OFFLINE;
    uint32_t backoff = 30000UL << (_login_fails > 4 ? 4 : _login_fails - 1);
    if (backoff > 300000UL) backoff = 300000UL;
    _next_login = now + backoff;
    _dirty = true;
  } else if (_link != LINK_LOGIN && (long)(now - _next_login) >= 0) {
    sendLoginNow();
  }

  if (_members_tag && (long)(now - _members_deadline) >= 0) {
    // no answer: retry sooner at first (20 s, 60 s, then every 2 min); the People screen says so
    _members_tag = 0;
    if (_members_fails < 255) _members_fails++;
    _members_state = MEMBERS_FAILED;
    _next_members = now + (_members_fails == 1 ? 20000UL : (_members_fails == 2 ? 60000UL : 120000UL));
    _dirty = true;
  }
  if (_link == LINK_ONLINE && !_members_tag && _next_members && (long)(now - _next_members) >= 0) {
    _next_members = now + 120000;   // overwritten when a reply arrives
    requestMembers(_members_next);
  }
}

void KidsMesh::gpsLoop() {
  gps.loop();
  unsigned long now = millis();
  if (!_gps_seen && (long)(now - _gps_baud_switch) >= 0) {
    char diag[128];
    gps.formatDiagnostics(diag, sizeof(diag));
    const char* ok = strstr(diag, "ok:");
    uint32_t okc = ok ? strtoul(ok + 3, NULL, 10) : 0;
    if (okc > 0) {
      _gps_seen = true;   // valid NMEA at this baud rate
    } else {
      _gps_baud = _gps_baud == 38400 ? 9600 : 38400;
      Serial1.updateBaudRate(_gps_baud);
      _gps_baud_switch = now + 4000;
    }
  }
}

void KidsMesh::locationLoop() {
  if ((long)(millis() - _next_gps_check) < 0) return;
  _next_gps_check = millis() + 10000;
  if (!_prefs.share_loc || _link != LINK_ONLINE || !gps.isValid()) return;
  if (gps.satellitesCount() < 4) return;
  int32_t lat = gps.getLatitude(), lon = gps.getLongitude();
  uint32_t now = upSecs() | 1;
  if (!kids::shouldSendLocation(_loc, now, lat, lon)) return;
  ContactInfo* r = room();
  if (!r) return;
  uint8_t buf[16];
  uint32_t fix_time = (uint32_t)gps.getTimestamp();
  size_t len = spark::locationReqEncode(buf, lat, lon, fix_time, (uint8_t)gps.satellitesCount());
  uint32_t tag = 0, est = 0;
  if (sendRequest(*r, buf, len, tag, est) == MSG_SEND_FAILED) return;
  _loc_tag = tag;
  _loc_lat = lat;
  _loc_lon = lon;
  _loc.tried_at = now;
}

// Advert with our name and no location (scoped flood): at boot, on a name change, every 6 h.
// The room, its bridge and phone apps learn the name from it.
void KidsMesh::sendAdvertNow() {
  _next_advert = millis() + ADVERT_INTERVAL_MS;
  if (!_prefs.name[0]) return;
  mesh::Packet* adv = createSelfAdvert(_prefs.name);
  if (!adv) return;
  if (!_scoped) { sendFlood(adv, 500UL, KIDS_PATH_HASH_SIZE); return; }
  uint16_t codes[2] = { _scope.calcTransportCode(adv), 0 };
  sendFlood(adv, codes, 500, KIDS_PATH_HASH_SIZE);
}

// Name change (first boot, "Change my name", or Settings). The identity (key pair) is kept.
void KidsMesh::applyName(const char* name) {
  char tmp[16];
  spark::memberCopyName(tmp, name, strlen(name));
  if (!tmp[0]) return;
  strcpy(_prefs.name, tmp);
  savePrefs();
  sendAdvertNow();
  // tell the room's member directory: GET_MEMBERS carries our name
  _members_next = 0;
  _members_tag = 0;
  _next_members = millis() + 2500;
  _dirty = true;
}

// =====================================================================================
// main loop

void KidsMesh::loop() {
  BaseChatMesh::loop();
  gpsLoop();
  linkLoop();
  checkPending();
  locationLoop();
  uiLoop();
  serialLoop();

  unsigned long now = millis();
  if (_next_advert && (long)(now - _next_advert) >= 0) sendAdvertNow();
  if ((long)(now - _next_batt) >= 0) {
    uint16_t mv = board.getBattMilliVolts();
    _batt_mv = _batt_mv == 0 ? mv : (uint16_t)((_batt_mv * 7 + mv) / 8);
    _next_batt = now + 5000;
    _dirty = true;   // also refreshes the clock
  }
  if ((long)(now - _next_floor_save) >= 0) {
    _next_floor_save = now + kids::CLOCK_FLOOR_SAVE_SECS * 1000UL;
    _prefs.clock_floor = getRTCClock()->getCurrentTime() + kids::CLOCK_FLOOR_MARGIN;
    savePrefs();
  }
  if (_msgs_save_at && (long)(now - _msgs_save_at) >= 0) { _msgs_save_at = 0; saveMsgs(); }
  if (_people_save_at && (long)(now - _people_save_at) >= 0) { _people_save_at = 0; savePeople(); }
}

