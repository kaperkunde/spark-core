#pragma once

// T-Deck Plus peripherals for the SpArkKids firmware: display (drawn into a PSRAM frame
// buffer, then pushed in one go), I2C keyboard, trackball, speaker, backlight.

#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>

#define SCREEN_W 320
#define SCREEN_H 240

// RGB565 colours
#define C_BLACK   0x0000
#define C_WHITE   0xFFFF
#define C_GREY    0x8410
#define C_DGREY   0x31A6
#define C_LGREY   0xC618
#define C_RED     0xF800
#define C_GREEN   0x07E0
#define C_YELLOW  0xFFE0
#define C_BLUE    0x041F
#define C_ORANGE  0xFC60
#define C_BAR     0x10A2
#define C_SEL     0x1A6B
#define C_OWN     0x9F1F

class FrameBuffer : public Adafruit_GFX {
  uint16_t* _buf;
public:
  FrameBuffer() : Adafruit_GFX(SCREEN_W, SCREEN_H), _buf(NULL) {}
  bool begin();
  uint16_t* buffer() { return _buf; }
  void drawPixel(int16_t x, int16_t y, uint16_t color) override;
  void fillScreen(uint16_t color) override;
  void drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t color) override;
  void drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t color) override;
  void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color) override;
};

// Key codes besides printable ASCII
#define KEY_ENTER 0x0D
#define KEY_BACKSPACE 0x08

enum TrackEvent { TB_NONE = 0, TB_UP, TB_DOWN, TB_LEFT, TB_RIGHT, TB_CLICK };

class Hw {
public:
  FrameBuffer fb;
  void begin();
  void flush();                 // push the frame buffer to the panel
  void backlight(bool on);
  bool isBacklightOn() const { return _bl_on; }

  int readKey();                // 0 = none
  TrackEvent readTrackball();   // one event per call

  void setVolume(uint8_t v) { _volume = v; }   // 0 = mute .. 3
  void beepRoom();
  void beepDM();
  void beepKey();
  void beepBack();
  void beepSend();
  void beepBump();
  void beepFail();

  // GT911 touch (polled; never writes the config registers)
  bool touchBegin();
  bool touchOk() const { return _touch_addr != 0; }
  uint8_t touchAddr() const { return _touch_addr; }
  // Reads one frame if due. Returns 1 with points/raw coordinates when a new frame arrived,
  // 0 when nothing new, -1 when touch is unavailable.
  int touchPoll(uint8_t& points, int16_t& raw_x, int16_t& raw_y);

private:
  bool _bl_on = false;
  uint8_t _volume = 2;
  unsigned long _click_down = 0;
  bool _click_prev = false;
  uint8_t _touch_addr = 0;
  uint8_t _touch_errs = 0;
  unsigned long _touch_next = 0;
  void tone(uint16_t f1, uint16_t ms1, uint16_t f2, uint16_t ms2);
};

extern Hw hw;
