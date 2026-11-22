#pragma once

// HostLink over the USB serial port, next to the text CLI.
//
// main.cpp's serial read loop gives bytes to feedByte() when the CLI line buffer is empty
// and the byte is '<' (or a frame is already in progress); those bytes are not echoed and
// never reach the line buffer. Complete frames are queued and handed out through
// checkRecvFrame() like any other BaseSerialInterface.

#include <Arduino.h>
#include <helpers/BaseSerialInterface.h>
#include "HostLinkCodec.h"

class UsbFrameInterface : public BaseSerialInterface {
public:
  static const unsigned long ACTIVE_WINDOW_MS = 120000;   // "a valid frame in the last 120 s"
  static const int QUEUE_LEN = 3;
  static const int TX_QUEUE_LEN = 4;

  UsbFrameInterface() : _q_len(0), _tx_len(0), _seen(false), _last_rx(0) { }

  // Returns true when the byte belongs to a frame (consumed, do not echo or buffer).
  bool feedByte(uint8_t b, bool line_empty) {
    if (!_parser.inFrame() && !(line_empty && b == '<')) return false;
    unsigned long now = millis();
    hl::UsbFrameParser::Result r = _parser.feed(b, now);
    if (r == hl::UsbFrameParser::FRAME) {
      if (_q_len < QUEUE_LEN) {
        _q[_q_len].len = (uint8_t)_parser.length();
        memcpy(_q[_q_len].buf, _parser.payload(), _parser.length());
        _q_len++;
      }   // else: dropped, the host retries on its own timeout
      _seen = true;
      _last_rx = now;
    }
    return true;
  }

  void enable() override { }
  void disable() override { }
  bool isEnabled() const override { return true; }
  bool isConnected() const override { return _seen && (millis() - _last_rx) < ACTIVE_WINDOW_MS; }
  // Outgoing frames wait in a small queue and are written only when the USB FIFO has room
  // for the whole frame, so a host that stops reading can never stall the mesh loop.
  void loop() override {
    _parser.tick(millis());
    if (_tx_len > 0 && !Serial) _tx_len = 0;   // nobody listening (no DTR): do not let stale frames pile up
    while (_tx_len > 0) {
      size_t n = _tx[0].len;
      if (Serial.availableForWrite() < (int)(n + 3)) break;
      uint8_t hdr[3] = { '>', (uint8_t)(n & 0xFF), (uint8_t)(n >> 8) };
      Serial.write(hdr, 3);
      Serial.write(_tx[0].buf, n);
      for (int i = 1; i < _tx_len; i++) _tx[i - 1] = _tx[i];
      _tx_len--;
    }
  }
  bool isWriteBusy() const override { return _tx_len >= TX_QUEUE_LEN * 2 / 3; }

  size_t writeFrame(const uint8_t src[], size_t len) override {
    if (len == 0 || len > hl::MAX_FRAME || _tx_len >= TX_QUEUE_LEN) return 0;
    _tx[_tx_len].len = (uint8_t)len;
    memcpy(_tx[_tx_len].buf, src, len);
    _tx_len++;
    return len;
  }

  size_t checkRecvFrame(uint8_t dest[]) override {
    if (_q_len == 0) return 0;
    size_t n = _q[0].len;
    memcpy(dest, _q[0].buf, n);
    for (int i = 1; i < _q_len; i++) _q[i - 1] = _q[i];
    _q_len--;
    return n;
  }

private:
  struct Slot { uint8_t len; uint8_t buf[hl::MAX_FRAME]; };
  hl::UsbFrameParser _parser;
  Slot _q[QUEUE_LEN];
  Slot _tx[TX_QUEUE_LEN];
  int _q_len, _tx_len;
  bool _seen;
  unsigned long _last_rx;
};
