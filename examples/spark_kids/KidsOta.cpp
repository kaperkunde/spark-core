#include "KidsOta.h"

#include <NimBLEDevice.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <esp_heap_caps.h>
#include <mbedtls/sha256.h>
#include <ed_25519.h>

// Set by spark/pio_env.py from .env: the public half of KIDS_OTA_SIGNING_KEY (64 hex digits) and
// the family tag (8 hex digits, a hash of the room key and that public key). Empty: no updates.
#ifndef KIDS_OTA_PUBKEY
  #define KIDS_OTA_PUBKEY ""
#endif
#ifndef KIDS_OTA_FAMILY
  #define KIDS_OTA_FAMILY "00000000"
#endif

using namespace kids::ota;

#define OTA_STATE_FILE "/kota"
#define OTA_STATE_MAGIC 0x41544F4BUL   // "KOTA"
#define RING_SIZE (32 * 1024)          // the bridge keeps at most 16 KB unacknowledged
#define WRITE_CHUNK 4096
#define ACK_EVERY 4096
#define DRAIN_PER_LOOP (16 * 1024)
#define PROMPT_SECS 120                // no tap: "no answer", the bridge tries again later
#define IDLE_DISCONNECT_MS 60000UL     // a connection that does nothing is dropped
#define RX_TIMEOUT_MS 30000UL
#define FAILED_SHOW_MS 5000UL
#define MIN_FREE_INTERNAL (70 * 1024)  // do not start Bluetooth below this much internal heap
#define ADV_INTERVAL 1600              // x 0.625 ms = 1 s (+0.1 s): still cheap; macOS scans with a low duty cycle

static const char* SERVICE_UUID = "8e5f0001-7c5a-4b39-9a0e-53704b694f54";
static const char* CTRL_UUID = "8e5f0002-7c5a-4b39-9a0e-53704b694f54";
static const char* DATA_UUID = "8e5f0003-7c5a-4b39-9a0e-53704b694f54";

struct OtaState {
  uint32_t magic;
  uint32_t snooze_until;   // epoch secs (RTC); the clock never goes back (persisted floor)
  uint8_t disabled;
  uint8_t rsvd[7];
};

static KidsOta* s_ota = nullptr;
static NimBLEServer* s_server = nullptr;
static NimBLECharacteristic* s_ctrl = nullptr;
static uint8_t s_pub[32], s_family[4];

class ServerCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* srv, ble_gap_conn_desc* desc) override {
    srv->updateConnParams(desc->conn_handle, 12, 24, 0, 500);   // 15..30 ms while connected
    if (s_ota) s_ota->onConnect(desc->conn_handle);
  }
  void onDisconnect(NimBLEServer*) override { if (s_ota) s_ota->onDisconnect(); }
};
class CtrlCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c) override {
    NimBLEAttValue v = c->getValue();
    if (s_ota) s_ota->onCtrl(v.data(), v.length());
  }
};
class DataCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c) override {
    NimBLEAttValue v = c->getValue();
    if (s_ota) s_ota->onData(v.data(), v.length());
  }
};

// ---------------------------------------------------------------------------------------------

bool KidsOta::keyPresent() const { return strlen(KIDS_OTA_PUBKEY) == 64; }

void KidsOta::begin(fs::FS& fs, uint32_t my_code, const char* version) {
  _fs = &fs;
  _my_code = my_code;
  shortLabel(version, _my_label, sizeof(_my_label));
  memset(&_offer, 0, sizeof(_offer));
  loadState();
  if (!keyPresent() || !kids::parseKey(KIDS_OTA_PUBKEY, s_pub)) {
    Serial.println("ota: no KIDS_OTA_SIGNING_KEY in this build, Bluetooth updates off");
    return;
  }
  for (int i = 0; i < 4; i++) {
    char h[3] = { KIDS_OTA_FAMILY[2 * i], KIDS_OTA_FAMILY[2 * i + 1], 0 };
    s_family[i] = (uint8_t)strtoul(h, NULL, 16);
  }
  if (_enabled) startBle();
}

bool KidsOta::startBle() {
  if (_ble_on) return true;
  size_t before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  if (before < MIN_FREE_INTERNAL) {
    Serial.printf("ota: only %u bytes internal heap free, Bluetooth stays off\n", (unsigned)before);
    return false;
  }
  if (!_ring) _ring = (uint8_t*)ps_malloc(RING_SIZE);
  if (!_ring) { Serial.println("ota: no PSRAM for the receive buffer"); return false; }
  s_ota = this;
  NimBLEDevice::init("SpArkKids");
  NimBLEDevice::setMTU(247);
  s_server = NimBLEDevice::createServer();
  s_server->setCallbacks(new ServerCb());
  s_server->advertiseOnDisconnect(true);
  NimBLEService* svc = s_server->createService(SERVICE_UUID);
  s_ctrl = svc->createCharacteristic(CTRL_UUID, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::NOTIFY,
                                     OFFER_LEN + LABEL_MAX);
  s_ctrl->setCallbacks(new CtrlCb());
  NimBLECharacteristic* data = svc->createCharacteristic(DATA_UUID, NIMBLE_PROPERTY::WRITE_NR, 512);
  data->setCallbacks(new DataCb());
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->setMinInterval(ADV_INTERVAL);
  adv->setMaxInterval(ADV_INTERVAL + 160);
  NimBLEAdvertisementData sr;
  sr.setCompleteServices(NimBLEUUID(SERVICE_UUID));
  adv->setScanResponseData(sr);
  _ble_on = true;
  _adv_flags = 0xFF;
  refreshStatusValue();
  updateAdvert();
  size_t after = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  Serial.printf("ota: Bluetooth on, code %lu, internal heap %u -> %u bytes free (largest block %u)\n",
                (unsigned long)_my_code, (unsigned)before, (unsigned)after,
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
  return true;
}

void KidsOta::setEnabled(bool on) {
  _enabled = on;
  saveState();
  if (on) {
    if (_ble_on) {
      s_server->advertiseOnDisconnect(true);
      _adv_flags = 0xFF;
      updateAdvert();
    } else if (keyPresent()) {
      startBle();
    }
  } else if (_ble_on) {
    // Stop advertising and drop any link now; the Bluetooth stack itself is not started at the
    // next boot.
    if (_phase != IDLE && _phase != DONE) fail(R_DISABLED);
    s_server->advertiseOnDisconnect(false);
    disconnect();
    NimBLEDevice::getAdvertising()->stop();
  }
}

void KidsOta::clearSnooze() { _snooze_until = 0; saveState(); }

void KidsOta::loadState() {
  OtaState s;
  memset(&s, 0, sizeof(s));
  if (_fs->exists(OTA_STATE_FILE)) {
    File f = _fs->open(OTA_STATE_FILE);
    if (f) {
      if (f.read((uint8_t*)&s, sizeof(s)) != sizeof(s) || s.magic != OTA_STATE_MAGIC) memset(&s, 0, sizeof(s));
      f.close();
    }
  }
  _snooze_until = s.snooze_until;
  _enabled = !s.disabled;
}

void KidsOta::saveState() {
  OtaState s;
  memset(&s, 0, sizeof(s));
  s.magic = OTA_STATE_MAGIC;
  s.snooze_until = _snooze_until;
  s.disabled = !_enabled;
  File f = _fs->open(OTA_STATE_FILE, "w", true);
  if (f) { f.write((const uint8_t*)&s, sizeof(s)); f.close(); }
}

uint8_t KidsOta::advFlags() const {
  uint8_t f = 0;
  if (_now && snoozeLeft(_now)) f |= ADV_SNOOZED;   // _now = 0: before the first loop()
  if (_now && !batteryOk(_batt_mv, _usb)) f |= ADV_LOW_BATTERY;
  if (_phase != IDLE) f |= ADV_BUSY;
  return f;
}

void KidsOta::updateAdvert() {
  if (!_ble_on || !_enabled) return;
  uint8_t flags = advFlags();
  if (flags == _adv_flags) return;
  _adv_flags = flags;
  uint8_t md[2 + ADVERT_LEN];
  md[0] = COMPANY_ID & 0xFF;
  md[1] = COMPANY_ID >> 8;
  encodeAdvert(md + 2, s_family, _my_code, flags);
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  NimBLEAdvertisementData ad;
  ad.setFlags(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);
  ad.setManufacturerData(std::string((const char*)md, sizeof(md)));
  bool was = adv->isAdvertising();
  if (was) adv->stop();
  adv->setAdvertisementData(ad);
  if (!_connected) adv->start();
}

void KidsOta::refreshStatusValue() {
  if (!s_ctrl) return;
  uint8_t buf[STATUS_LEN + LABEL_MAX];
  size_t n = encodeStatus(buf, sizeof(buf), _my_code, advFlags(), snoozeLeft(_now), _batt_mv, _my_label);
  s_ctrl->setValue(buf, n);
}

void KidsOta::notifyState(uint8_t state, uint8_t reason, uint32_t value) {
  if (!s_ctrl || !_connected) return;
  uint8_t b[7];
  encodeState(b, state, reason, value);
  s_ctrl->notify(b, sizeof(b));
}

void KidsOta::notifyOffset(uint8_t op, uint32_t off) {
  if (!s_ctrl || !_connected) return;
  uint8_t b[5];
  encodeOffset(b, op, off);
  s_ctrl->notify(b, sizeof(b));
}

void KidsOta::disconnect() {
  if (s_server && _connected) s_server->disconnect(_conn);
}

// --- Bluetooth task callbacks (keep short: copy, flag, return) --------------------------------

void KidsOta::onConnect(uint16_t conn) { _conn = conn; _connected = true; _conn_event = true; }
void KidsOta::onDisconnect() { _connected = false; _disc_event = true; }

void KidsOta::onCtrl(const uint8_t* data, size_t len) {
  if (_cmd_len || len == 0 || len > sizeof(_cmd)) return;   // one command at a time
  memcpy(_cmd, data, len);
  __sync_synchronize();
  _cmd_len = len;
}

void KidsOta::onData(const uint8_t* data, size_t len) {
  if (_phase != RECEIVING || len <= 4 || !_ring) return;
  uint32_t off = (uint32_t)data[0] | ((uint32_t)data[1] << 8) | ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);
  uint32_t n = len - 4;
  RxResult r = rxCheck(_rx_next, _offer.size, off, n);
  if (r == RX_DUP) return;
  if (r != RX_OK || (_ring_head - _ring_tail) + n > RING_SIZE) { _nack = true; return; }
  uint32_t pos = _ring_head % RING_SIZE, first = n < RING_SIZE - pos ? n : RING_SIZE - pos;
  memcpy(_ring + pos, data + 4, first);
  if (first < n) memcpy(_ring, data + 4 + first, n - first);
  __sync_synchronize();
  _ring_head = _ring_head + n;
  _rx_next = off + n;
}

// --- main loop ----------------------------------------------------------------------------------

void KidsOta::setPhase(Phase p) {
  _phase = p;
  _phase_at = millis();
  _last_activity = millis();
  updateAdvert();
}

void KidsOta::loop(uint32_t now, uint16_t batt_mv, bool usb) {
  _now = now;
  _batt_mv = batt_mv;
  _usb = usb;
  if (!_ble_on) return;
  unsigned long ms = millis();

  if (_conn_event) { _conn_event = false; _last_activity = ms; }
  if (_disc_event) {
    _disc_event = false;
    if (_phase == PROMPT) setPhase(IDLE);
    else if (_phase == RECEIVING || _phase == FINISHING) fail(R_ABORTED, false);
  }
  if (_cmd_len) {
    uint8_t c[sizeof(_cmd)];
    size_t n = _cmd_len;
    memcpy(c, _cmd, n);
    __sync_synchronize();
    _cmd_len = 0;
    _last_activity = ms;
    handleCmd(c, n);
  }
  ms = millis();   // the command may have started a phase just now (no unsigned wrap below)

  switch (_phase) {
    case PROMPT:
      if ((long)(ms - _phase_at) > (long)(PROMPT_SECS * 1000UL)) { notifyState(ST_NOANSWER, R_TIMEOUT, 0); setPhase(IDLE); }
      break;
    case RECEIVING:
      drain();
      if (_phase == RECEIVING && (long)(ms - _last_activity) > (long)RX_TIMEOUT_MS) fail(R_TIMEOUT);
      break;
    case FAILED:
      if ((long)(ms - _phase_at) > (long)FAILED_SHOW_MS) setPhase(IDLE);
      break;
    default:
      break;
  }
  if (_connected && _phase == IDLE && (long)(ms - _last_activity) > (long)IDLE_DISCONNECT_MS) { disconnect(); _last_activity = ms; }

  static unsigned long next_refresh = 0;
  if ((long)(ms - next_refresh) >= 0) {
    next_refresh = ms + 5000;
    updateAdvert();          // snooze over, battery state changed
    refreshStatusValue();
  }
}

void KidsOta::handleCmd(const uint8_t* c, size_t n) {
  switch (c[0]) {
    case CMD_OFFER: handleOffer(c, n); break;
    case CMD_ABORT:
      if (_phase == PROMPT) setPhase(IDLE);
      else if (_phase == RECEIVING) fail(R_ABORTED, false);
      break;
    case CMD_QUERY: {
      uint32_t left = snoozeLeft(_now);
      if (left) notifyState(ST_SNOOZED, R_NONE, left);
      else if (_phase != IDLE) notifyState(ST_BUSY, R_NONE, 0);
      break;
    }
    default: break;   // CMD_FINISH: the image is finished when its last byte is written
  }
}

void KidsOta::handleOffer(const uint8_t* c, size_t n) {
  Offer o;
  if (!_enabled) { notifyState(ST_REFUSED, R_DISABLED, 0); return; }
  if (_phase != IDLE) { notifyState(ST_BUSY, R_NONE, 0); return; }
  if (!parseOffer(c, n, o)) { notifyState(ST_REFUSED, R_BAD_OFFER, 0); return; }
  const esp_partition_t* part = esp_ota_get_next_update_partition(NULL);
  uint8_t r = checkOffer(o, _my_code, part ? part->size : 0);
  if (r) { notifyState(ST_REFUSED, r, 0); return; }
  uint8_t msg[SIGNED_LEN];
  signedMessage(msg, o.code, o.size, o.sha);
  if (!ed25519_verify(o.sig, msg, sizeof(msg), s_pub)) {
    Serial.println("ota: offer with a bad signature refused");
    notifyState(ST_REFUSED, R_BAD_SIGNATURE, 0);
    return;
  }
  uint32_t left = snoozeLeft(_now);
  if (left) { notifyState(ST_SNOOZED, R_NONE, left); return; }
  if (!batteryOk(_batt_mv, _usb)) { notifyState(ST_REFUSED, R_LOW_BATTERY, _batt_mv); return; }
  _offer = o;
  _low_batt_note = false;
  setPhase(PROMPT);
  _prompt_event = true;
  Serial.printf("ota: offer %s (code %lu, %lu bytes): prompt shown\n", o.label, (unsigned long)o.code,
                (unsigned long)o.size);
  notifyState(ST_PROMPT, R_NONE, PROMPT_SECS);
}

void KidsOta::install() {
  if (_phase != PROMPT) return;
  if (!batteryOk(_batt_mv, _usb)) {
    _low_batt_note = true;
    notifyState(ST_REFUSED, R_LOW_BATTERY, _batt_mv);
    fail(R_LOW_BATTERY, false);
    return;
  }
  if (!beginFlash()) { fail(R_FLASH_ERROR); return; }
  setPhase(RECEIVING);
  Serial.printf("ota: installing %s\n", _offer.label);
  notifyState(ST_READY, R_NONE, 0);
}

void KidsOta::later() {
  if (_phase != PROMPT) return;
  _snooze_until = _now + SNOOZE_SECS;
  saveState();
  notifyState(ST_SNOOZED, R_NONE, SNOOZE_SECS);
  setPhase(IDLE);
  Serial.println("ota: snoozed for 12 h");
}

bool KidsOta::beginFlash() {
  const esp_partition_t* part = esp_ota_get_next_update_partition(NULL);
  if (!part || part->size < _offer.size) return false;
  esp_ota_handle_t h = 0;
  if (esp_ota_begin(part, OTA_WITH_SEQUENTIAL_WRITES, &h) != ESP_OK) return false;
  _part = part;
  _handle = h;
  if (!_sha) _sha = malloc(sizeof(mbedtls_sha256_context));
  if (!_sha) { esp_ota_abort(h); return false; }
  mbedtls_sha256_init((mbedtls_sha256_context*)_sha);
  mbedtls_sha256_starts_ret((mbedtls_sha256_context*)_sha, 0);
  _written = _acked = 0;
  _ring_head = _ring_tail = 0;
  _rx_next = 0;
  _nack = false;
  return true;
}

void KidsOta::drain() {
  static uint8_t chunk[WRITE_CHUNK];
  static unsigned long last_nack = 0;
  uint32_t budget = DRAIN_PER_LOOP;
  while (budget > 0) {
    uint32_t avail = _ring_head - _ring_tail;
    bool last = _written + avail >= _offer.size;
    if (avail == 0 || (avail < WRITE_CHUNK && !last)) break;
    uint32_t n = avail < WRITE_CHUNK ? avail : WRITE_CHUNK;
    uint32_t pos = _ring_tail % RING_SIZE, first = n < RING_SIZE - pos ? n : RING_SIZE - pos;
    memcpy(chunk, _ring + pos, first);
    if (first < n) memcpy(chunk + first, _ring, n - first);
    __sync_synchronize();
    _ring_tail = _ring_tail + n;
    mbedtls_sha256_update_ret((mbedtls_sha256_context*)_sha, chunk, n);
    if (esp_ota_write((esp_ota_handle_t)_handle, chunk, n) != ESP_OK) { fail(R_FLASH_ERROR); return; }
    _written += n;
    _last_activity = millis();
    budget -= n;
  }
  if (_written - _acked >= ACK_EVERY || (_written == _offer.size && _acked != _written)) {
    _acked = _written;
    notifyOffset(NTF_ACK, _written);
  }
  if (_nack && millis() - last_nack > 300) {
    _nack = false;
    last_nack = millis();
    notifyOffset(NTF_NACK, _rx_next);
  }
  if (_written == _offer.size) finish();
}

void KidsOta::finish() {
  setPhase(FINISHING);
  uint8_t sha[32];
  mbedtls_sha256_finish_ret((mbedtls_sha256_context*)_sha, sha);
  mbedtls_sha256_free((mbedtls_sha256_context*)_sha);
  if (memcmp(sha, _offer.sha, 32) != 0) { fail(R_HASH_MISMATCH); return; }
  esp_ota_handle_t h = (esp_ota_handle_t)_handle;
  _handle = 0;
  if (esp_ota_end(h) != ESP_OK) { _part = nullptr; fail(R_FLASH_ERROR); return; }
  if (esp_ota_set_boot_partition((const esp_partition_t*)_part) != ESP_OK) { _part = nullptr; fail(R_FLASH_ERROR); return; }
  _part = nullptr;
  Serial.printf("ota: %s written and verified, restarting\n", _offer.label);
  notifyState(ST_DONE, R_NONE, _offer.code);
  setPhase(DONE);
  _reboot_at = millis() + 2000;
}

bool KidsOta::rebootDue() const { return _phase == DONE && (long)(millis() - _reboot_at) >= 0; }

void KidsOta::endFlash(bool abort) {
  if (_handle) { if (abort) esp_ota_abort((esp_ota_handle_t)_handle); _handle = 0; }
  _part = nullptr;
}

void KidsOta::fail(uint8_t reason, bool notify) {
  bool was_flash = _phase == RECEIVING || _phase == FINISHING;
  if (was_flash) endFlash(true);
  if (notify) notifyState(ST_FAILED, reason, _written);
  _fail_reason = reason;
  Serial.printf("ota: update failed (%s)\n", reasonText(reason));
  setPhase(FAILED);
}

void KidsOta::printStatus(Print& out, uint32_t now) const {
  const esp_partition_t* run = esp_ota_get_running_partition();
  out.printf("ota code %lu label %s key %s enabled %d ble %d phase %d snooze %lus batt %umV usb %d\n",
             (unsigned long)_my_code, _my_label, keyPresent() ? "yes" : "no", _enabled, _ble_on, (int)_phase,
             (unsigned long)snoozeLeft(now), _batt_mv, _usb);
  out.printf("ota family %s adv %d flags 0x%02X conn %d running %s heap internal %u (largest %u) psram %u\n",
             KIDS_OTA_FAMILY, _ble_on ? (int)NimBLEDevice::getAdvertising()->isAdvertising() : 0, _adv_flags,
             (int)_connected, run ? run->label : "?", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

bool kidsOtaMarkValid() {
  const esp_partition_t* run = esp_ota_get_running_partition();
  esp_ota_img_states_t st;
  if (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
    esp_ota_mark_app_valid_cancel_rollback();
    Serial.printf("ota: new image in %s marked valid\n", run->label);
    return true;
  }
  return false;
}

// Arduino marks a new image valid right at boot unless this returns true; we do it ourselves
// once the app has run for a while (kidsOtaMarkValid), so a build that crashes early rolls back.
extern "C" bool verifyRollbackLater() { return true; }
