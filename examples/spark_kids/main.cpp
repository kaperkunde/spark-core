// SparkKids: a one-room MeshCore messenger for the LilyGO T-Deck Plus.
// Room = your SparkNode (key from .env), People = its member directory (GET_MEMBERS), DMs to members.

#include <Arduino.h>
#include <SPIFFS.h>
#include "KidsApp.h"
#include "Hw.h"

StdRNG fast_rng;
SimpleMeshTables tables;
KidsMesh the_mesh(radio_driver, fast_rng, rtc_clock, tables);

void setup() {
  Serial.begin(115200);
  board.begin();
  delay(200);   // let the peripheral rail (keyboard, display, GPS) come up

  if (!radio_init()) {
    hw.begin();
    hw.fb.setTextColor(C_RED);
    hw.fb.setTextSize(2);
    hw.fb.setCursor(10, 100);
    hw.fb.print("Radio error");
    hw.flush();
    while (1) delay(1000);
  }
  hw.begin();
  fast_rng.begin(radio_driver.getRngSeed());

  SPIFFS.begin(true);
  the_mesh.begin(SPIFFS);

  radio_driver.setParams(LORA_FREQ, LORA_BW, LORA_SF, LORA_CR);
  radio_driver.setTxPower(LORA_TX_POWER);
}

void loop() {
  the_mesh.loop();
  rtc_clock.tick();
}
