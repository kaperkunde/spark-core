#include "Hw.h"
#include <target.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#ifndef KIDS_ROTATION
  #define KIDS_ROTATION 3   // landscape, keyboard below the screen (1 is upside down)
#endif

#define KB_ADDR 0x55
#define TB_UP_PIN 3
#define TB_DOWN_PIN 15
#define TB_LEFT_PIN 1
#define TB_RIGHT_PIN 2
#define TB_CLICK_PIN 0
#define I2S_BCK 7
#define I2S_WS 5
#define I2S_DOUT 6

#define TB_STEPS_V 2   // edges per up/down event
#ifndef TB_STEPS_H
  #define TB_STEPS_H 6   // edges per left/right event (less accidental screen changes)
#endif
#ifndef TOUCH_ADDR_A
  #define TOUCH_ADDR_A 0x5D
#endif
#ifndef TOUCH_ADDR_B
  #define TOUCH_ADDR_B 0x14
#endif
#define TOUCH_POLL_MS 20

Hw hw;
static Adafruit_ST7789 tft(&shared_spi, PIN_TFT_CS, PIN_TFT_DC, -1);

// ------------------------------------------------------------------ frame buffer

bool FrameBuffer::begin() {
  _buf = (uint16_t*)ps_malloc(SCREEN_W * SCREEN_H * 2);
  if (!_buf) _buf = (uint16_t*)malloc(SCREEN_W * SCREEN_H * 2);
  if (_buf) memset(_buf, 0, SCREEN_W * SCREEN_H * 2);
  return _buf != NULL;
}
void FrameBuffer::drawPixel(int16_t x, int16_t y, uint16_t color) {
  if (!_buf || x < 0 || y < 0 || x >= SCREEN_W || y >= SCREEN_H) return;
  _buf[y * SCREEN_W + x] = color;
}
void FrameBuffer::fillScreen(uint16_t color) {
  if (!_buf) return;
  for (int i = 0; i < SCREEN_W * SCREEN_H; i++) _buf[i] = color;
}
void FrameBuffer::drawFastHLine(int16_t x, int16_t y, int16_t w, uint16_t color) { fillRect(x, y, w, 1, color); }
void FrameBuffer::drawFastVLine(int16_t x, int16_t y, int16_t h, uint16_t color) { fillRect(x, y, 1, h, color); }
void FrameBuffer::fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color) {
  if (!_buf) return;
  if (x < 0) { w += x; x = 0; }
  if (y < 0) { h += y; y = 0; }
  if (x + w > SCREEN_W) w = SCREEN_W - x;
  if (y + h > SCREEN_H) h = SCREEN_H - y;
  if (w <= 0 || h <= 0) return;
  for (int j = y; j < y + h; j++) {
    uint16_t* p = &_buf[j * SCREEN_W + x];
    for (int i = 0; i < w; i++) p[i] = color;
  }
}

// ------------------------------------------------------------------ trackball

static volatile int16_t tb_up, tb_down, tb_left, tb_right;
static void IRAM_ATTR isr_up() { tb_up++; }
static void IRAM_ATTR isr_down() { tb_down++; }
static void IRAM_ATTR isr_left() { tb_left++; }
static void IRAM_ATTR isr_right() { tb_right++; }

// ------------------------------------------------------------------ sound

struct ToneReq { uint16_t f1, ms1, f2, ms2; uint8_t vol; };
static QueueHandle_t tone_q;

static void play(uint16_t f, uint16_t ms, uint8_t vol) {
  const int rate = 16000;
  static int16_t buf[256 * 2];
  int total = rate * ms / 1000;
  int amp = vol == 0 ? 0 : (vol == 1 ? 1500 : (vol == 2 ? 5000 : 12000));
  int half = f ? rate / (2 * f) : rate;
  int phase = 0;
  while (total > 0) {
    int n = total > 256 ? 256 : total;
    for (int i = 0; i < n; i++) {
      // square wave with a short fade in/out against clicks
      int16_t s = ((phase / half) & 1) ? amp : -amp;
      phase++;
      buf[2 * i] = buf[2 * i + 1] = s;
    }
    size_t written;
    i2s_write(I2S_NUM_0, buf, n * 4, &written, portMAX_DELAY);
    total -= n;
  }
}

static void sound_task(void*) {
  ToneReq r;
  for (;;) {
    if (xQueueReceive(tone_q, &r, portMAX_DELAY) == pdTRUE) {
      play(r.f1, r.ms1, r.vol);
      if (r.ms2) { play(0, 40, 0); play(r.f2, r.ms2, r.vol); }
      play(0, 60, 0);   // flush silence so the amp does not hold the last sample
      i2s_zero_dma_buffer(I2S_NUM_0);
    }
  }
}

static void sound_begin() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = 16000;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = 0;
  cfg.dma_buf_count = 4;
  cfg.dma_buf_len = 256;
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;
  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL) != ESP_OK) return;
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = I2S_BCK;
  pins.ws_io_num = I2S_WS;
  pins.data_out_num = I2S_DOUT;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  i2s_set_pin(I2S_NUM_0, &pins);
  i2s_zero_dma_buffer(I2S_NUM_0);
  tone_q = xQueueCreate(4, sizeof(ToneReq));
  xTaskCreatePinnedToCore(sound_task, "snd", 3072, NULL, 1, NULL, 0);
}

void Hw::tone(uint16_t f1, uint16_t ms1, uint16_t f2, uint16_t ms2) {
  if (!tone_q || _volume == 0) return;
  ToneReq r = { f1, ms1, f2, ms2, _volume };
  xQueueSend(tone_q, &r, 0);
}
void Hw::beepRoom() { tone(880, 90, 0, 0); }
void Hw::beepDM() { tone(988, 90, 1319, 140); }
void Hw::beepKey() { tone(1760, 8, 0, 0); }
void Hw::beepBack() { tone(660, 30, 440, 30); }
void Hw::beepSend() { tone(880, 40, 1175, 40); }
void Hw::beepBump() { tone(300, 15, 0, 0); }
void Hw::beepFail() { tone(220, 150, 0, 0); }

// ------------------------------------------------------------------ touch (GT911)

static bool gt_read(uint8_t addr, uint16_t reg, uint8_t* buf, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, n) != n) return false;
  for (int i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}
static bool gt_write8(uint8_t addr, uint16_t reg, uint8_t v) {
  Wire.beginTransmission(addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  Wire.write(v);
  return Wire.endTransmission() == 0;
}

bool Hw::touchBegin() {
  pinMode(16, INPUT);   // INT: not used for events (unreliable on the Plus), polled instead
  const uint8_t addrs[2] = { TOUCH_ADDR_A, TOUCH_ADDR_B };
  for (int attempt = 0; attempt < 2 && !_touch_addr; attempt++) {
    if (attempt) delay(300);
    for (int i = 0; i < 2; i++) {
      uint8_t id[4];
      if (gt_read(addrs[i], 0x8140, id, 4)) {
        _touch_addr = addrs[i];
        gt_write8(_touch_addr, 0x814E, 0);
        Serial.printf("touch: GT%c%c%c at 0x%02X\n", id[0], id[1], id[2], _touch_addr);
        break;
      }
    }
  }
  if (!_touch_addr) Serial.println("touch: not found");
  return _touch_addr != 0;
}

int Hw::touchPoll(uint8_t& points, int16_t& raw_x, int16_t& raw_y) {
  if (!_touch_addr) return -1;
  if ((long)(millis() - _touch_next) < 0) return 0;
  _touch_next = millis() + (_bl_on ? TOUCH_POLL_MS : 50);
  uint8_t b[9];
  if (!gt_read(_touch_addr, 0x814E, b, 9)) {
    if (++_touch_errs >= 10) { _touch_addr = 0; Serial.println("touch: lost"); }
    else if (_touch_errs % 3 == 0) _touch_next = millis() + 1000;
    return 0;
  }
  _touch_errs = 0;
  if (!(b[0] & 0x80)) return 0;   // no new frame
  points = b[0] & 0x0F;
  raw_x = (int16_t)(b[2] | (b[3] << 8));
  raw_y = (int16_t)(b[4] | (b[5] << 8));
  gt_write8(_touch_addr, 0x814E, 0);
  if (points > 5) points = 0;   // garbage
  return 1;
}

// ------------------------------------------------------------------ setup

void Hw::begin() {
  fb.begin();

  pinMode(TB_UP_PIN, INPUT_PULLUP);
  pinMode(TB_DOWN_PIN, INPUT_PULLUP);
  pinMode(TB_LEFT_PIN, INPUT_PULLUP);
  pinMode(TB_RIGHT_PIN, INPUT_PULLUP);
  pinMode(TB_CLICK_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(TB_UP_PIN), isr_up, FALLING);
  attachInterrupt(digitalPinToInterrupt(TB_DOWN_PIN), isr_down, FALLING);
  attachInterrupt(digitalPinToInterrupt(TB_LEFT_PIN), isr_left, FALLING);
  attachInterrupt(digitalPinToInterrupt(TB_RIGHT_PIN), isr_right, FALLING);

  pinMode(PIN_TFT_LEDA_CTL, OUTPUT);
  digitalWrite(PIN_TFT_LEDA_CTL, LOW);
  tft.init(240, 320);
  tft.setSPISpeed(40000000);
  tft.setRotation(KIDS_ROTATION);
  tft.fillScreen(ST77XX_BLACK);
  backlight(true);

  sound_begin();
  touchBegin();
}

void Hw::flush() {
  if (!fb.buffer()) return;
  tft.drawRGBBitmap(0, 0, fb.buffer(), SCREEN_W, SCREEN_H);
}

void Hw::backlight(bool on) {
  _bl_on = on;
  digitalWrite(PIN_TFT_LEDA_CTL, on ? HIGH : LOW);
}

int Hw::readKey() {
  static unsigned long next_poll = 0;
  if ((long)(millis() - next_poll) < 0) return 0;
  next_poll = millis() + 15;
  if (Wire.requestFrom(KB_ADDR, 1) != 1) { next_poll = millis() + 1000; return 0; }   // keyboard absent/booting
  int c = Wire.read();
  return c < 0 ? 0 : c;
}

TrackEvent Hw::readTrackball() {
  bool down = digitalRead(TB_CLICK_PIN) == LOW;
  TrackEvent ev = TB_NONE;
  if (down && !_click_prev) _click_down = millis();
  if (!down && _click_prev && millis() - _click_down > 30) ev = TB_CLICK;   // on release
  _click_prev = down;
  if (ev != TB_NONE) return ev;

  // forget partial movements, so a slow drift never adds up to a screen change
  static unsigned long next_decay = 0;
  if ((long)(millis() - next_decay) >= 0) {
    next_decay = millis() + 500;
    noInterrupts();
    if (tb_left < TB_STEPS_H) tb_left = 0;
    if (tb_right < TB_STEPS_H) tb_right = 0;
    if (tb_up < TB_STEPS_V) tb_up = 0;
    if (tb_down < TB_STEPS_V) tb_down = 0;
    interrupts();
  }
  noInterrupts();
  int16_t u = tb_up, d = tb_down, l = tb_left, r = tb_right;
  interrupts();
  if (u >= TB_STEPS_V) { noInterrupts(); tb_up = 0; tb_down = 0; interrupts(); return TB_UP; }
  if (d >= TB_STEPS_V) { noInterrupts(); tb_down = 0; tb_up = 0; interrupts(); return TB_DOWN; }
  if (l >= TB_STEPS_H) { noInterrupts(); tb_left = 0; tb_right = 0; interrupts(); return TB_LEFT; }
  if (r >= TB_STEPS_H) { noInterrupts(); tb_right = 0; tb_left = 0; interrupts(); return TB_RIGHT; }
  return TB_NONE;
}
