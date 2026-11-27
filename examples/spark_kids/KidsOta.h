#pragma once

// Bluetooth firmware update for SpArkKids ("kids OTA").
//
// The T-Deck advertises (slowly) a small GATT service with its version code and a family tag.
// A SpArk Room Bridge of the same family that has a newer signed bundle connects and offers it;
// the kid (or a parent) sees "Update available" with Install / Later. Later snoozes offers for
// 12 hours (kept across reboots). Install streams the image into the inactive OTA slot; the
// hash and the Ed25519 signature (public key compiled in, .env KIDS_OTA_SIGNING_KEY) are checked
// before the boot partition is switched. The new image is marked valid after it has run for a
// while (bootloader rollback otherwise). Protocol constants: KidsLogic.h (kids::ota) and
// host/src/spark_bridge/kidsfw.py.

#include <Arduino.h>
#include <FS.h>
#include "KidsLogic.h"

class KidsOta {
public:
  enum Phase : uint8_t { IDLE, PROMPT, RECEIVING, FINISHING, DONE, FAILED };

  // Starts Bluetooth when a public key is compiled in, updates are enabled and the heap allows it.
  void begin(fs::FS& fs, uint32_t my_code, const char* version);
  // Call from the main loop. `now` = epoch seconds (RTC), batt_mv/usb for the battery rule.
  void loop(uint32_t now, uint16_t batt_mv, bool usb);

  Phase phase() const { return _phase; }
  const kids::ota::Offer& offer() const { return _offer; }
  const char* myLabel() const { return _my_label; }
  uint8_t progressPct() const { return _offer.size ? (uint8_t)((uint64_t)_written * 100 / _offer.size) : 0; }
  uint8_t failReason() const { return _fail_reason; }
  bool takePromptEvent() { bool e = _prompt_event; _prompt_event = false; return e; }
  bool rebootDue() const;

  // the prompt's buttons (also the serial test hooks)
  void install();
  void later();
  bool lowBatteryShown() const { return _low_batt_note; }

  // settings
  bool keyPresent() const;
  bool enabled() const { return _enabled; }
  void setEnabled(bool on);
  bool bleOn() const { return _ble_on; }
  uint32_t snoozeLeft(uint32_t now) const { return kids::ota::snoozeLeft(_snooze_until, now); }
  void clearSnooze();
  uint32_t code() const { return _my_code; }
  void printStatus(Print& out, uint32_t now) const;

  // called from the Bluetooth task (KidsOta.cpp)
  void onConnect(uint16_t conn);
  void onDisconnect();
  void onCtrl(const uint8_t* data, size_t len);
  void onData(const uint8_t* data, size_t len);

private:
  fs::FS* _fs = nullptr;
  uint32_t _my_code = 0;
  char _my_label[kids::ota::LABEL_MAX + 1] = { 0 };
  bool _enabled = true, _ble_on = false;
  uint32_t _snooze_until = 0;
  uint32_t _now = 0;
  uint16_t _batt_mv = 0;
  bool _usb = false;
  uint8_t _adv_flags = 0xFF;

  Phase _phase = IDLE;
  kids::ota::Offer _offer;
  bool _prompt_event = false, _low_batt_note = false;
  unsigned long _phase_at = 0, _last_activity = 0, _reboot_at = 0;
  uint8_t _fail_reason = 0;

  // flash
  const void* _part = nullptr;   // esp_partition_t*
  uint32_t _handle = 0;          // esp_ota_handle_t
  void* _sha = nullptr;          // mbedtls_sha256_context*
  uint32_t _written = 0, _acked = 0;

  // Bluetooth task -> loop (single producer / single consumer)
  volatile bool _connected = false, _conn_event = false, _disc_event = false;
  volatile uint16_t _conn = 0;
  uint8_t _cmd[kids::ota::OFFER_LEN + kids::ota::LABEL_MAX];
  volatile size_t _cmd_len = 0;   // > 0: a command waits
  uint8_t* _ring = nullptr;       // received image bytes (PSRAM)
  volatile uint32_t _rx_next = 0;  // next offset expected from the bridge
  volatile uint32_t _ring_head = 0, _ring_tail = 0;   // absolute byte counts in / out
  volatile bool _nack = false;

  void loadState();
  void saveState();
  bool startBle();
  void updateAdvert();
  uint8_t advFlags() const;
  void notifyState(uint8_t state, uint8_t reason, uint32_t value);
  void notifyOffset(uint8_t op, uint32_t off);
  void handleCmd(const uint8_t* c, size_t n);
  void handleOffer(const uint8_t* c, size_t n);
  bool beginFlash();
  void drain();
  void finish();
  void fail(uint8_t reason, bool notify = true);
  void endFlash(bool abort);
  void setPhase(Phase p);
  void refreshStatusValue();
  void disconnect();
};

// Marks a freshly updated image as valid (cancels the bootloader rollback). Call once the app has
// run for a while. Returns true when the running image was pending verification.
bool kidsOtaMarkValid();
