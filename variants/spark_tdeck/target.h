#pragma once

// spark-core: T-Deck Plus target for the SparkKids firmware. Same hardware wiring as
// variants/lilygo_tdeck, but the LoRa SPI bus is shared with the display (one SPIClass),
// and there is no upstream DISPLAY_CLASS / sensor manager: the kids' app drives the
// display, keyboard, trackball, speaker and GPS itself.

#define RADIOLIB_STATIC_ONLY 1
#include <RadioLib.h>
#include <SPI.h>
#include <helpers/radiolib/RadioLibWrappers.h>
#include <helpers/radiolib/CustomSX1262Wrapper.h>
#include <TDeckBoard.h>
#include <helpers/AutoDiscoverRTCClock.h>
#include <helpers/sensors/MicroNMEALocationProvider.h>
#include <helpers/SensorManager.h>

// Only exists so ESP32Board's sleep code can stop the GPS.
class KidsSensors : public SensorManager {
public:
  LocationProvider* getLocationProvider() override;
};

extern TDeckBoard board;
extern WRAPPER_CLASS radio_driver;
extern AutoDiscoverRTCClock rtc_clock;
extern MicroNMEALocationProvider gps;
extern KidsSensors sensors;
extern SPIClass shared_spi;   // LoRa + display + SD

bool radio_init();
mesh::LocalIdentity radio_new_identity();
