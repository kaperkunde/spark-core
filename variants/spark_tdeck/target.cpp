#include <Arduino.h>
#include "target.h"

TDeckBoard board;

SPIClass shared_spi(FSPI);
RADIO_CLASS radio = new Module(P_LORA_NSS, P_LORA_DIO_1, P_LORA_RESET, P_LORA_BUSY, shared_spi);

WRAPPER_CLASS radio_driver(radio, board);

ESP32RTCClock fallback_clock;
AutoDiscoverRTCClock rtc_clock(fallback_clock);
MicroNMEALocationProvider gps(Serial1, &rtc_clock);   // sets the clock from GPS time once it has a fix

KidsSensors sensors;
LocationProvider* KidsSensors::getLocationProvider() { return &gps; }

bool radio_init() {
  fallback_clock.begin();
  // ESP32Board::begin() already started Wire on the default pins (SDA 8 / SCL 9);
  // the T-Deck bus (keyboard 0x55, touch, sensors) is SDA 18 / SCL 8.
  Wire.end();
  Wire.begin(18, 8);
  Wire.setTimeOut(20);
  rtc_clock.begin(Wire);

  // Display CS and SD CS must be high before the bus is used, or they answer LoRa traffic.
  pinMode(PIN_TFT_CS, OUTPUT); digitalWrite(PIN_TFT_CS, HIGH);
  pinMode(PIN_SD_CS, OUTPUT); digitalWrite(PIN_SD_CS, HIGH);
  pinMode(P_LORA_NSS, OUTPUT); digitalWrite(P_LORA_NSS, HIGH);
  shared_spi.begin(P_LORA_SCLK, P_LORA_MISO, P_LORA_MOSI);
  return radio.std_init(&shared_spi);
}

mesh::LocalIdentity radio_new_identity() {
  RadioNoiseListener rng(radio);
  return mesh::LocalIdentity(&rng);
}
