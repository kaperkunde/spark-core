#pragma once

// HostLink: a subset of the MeshCore companion frame protocol served by the room server,
// so a host computer can bridge the room to something else over BLE or USB.
// Contract: spark/HOSTLINK.md. Compiled only with WITH_HOSTLINK.
//
// MyMesh and main.cpp reach it through these free functions only, so the upstream files
// carry a handful of one-line hooks.

#include <stdint.h>

class MyMesh;

// main.cpp, after the_mesh.begin(): load spark prefs, start BLE if configured.
void hostlink_setup(MyMesh& mesh);
// main.cpp, serial read loop. True if the byte was consumed (do not echo or buffer it).
bool hostlink_usb_byte(uint8_t c, bool line_empty);
// MyMesh::loop()
void hostlink_loop();
// MyMesh::storePost()
void hostlink_post_stored();
// MyMesh::handleCommand(), before the common CLI. True if `command` was a spark command
// (reply filled in).
bool hostlink_command(uint32_t sender_timestamp, char* command, char* reply);
// MyMesh::logRxRaw(): every received packet (raw bytes). Pushed as LOG_RX_DATA to hosts that
// enabled HOST_FEATURE_RX_LOG; never blocks (dropped when the transport is busy).
void hostlink_rx_raw(float snr, float rssi, const uint8_t* raw, int len);
// Member directory: a member's name changed (GET_MEMBERS). Updates the name cache and pushes
// NEW_ADVERT to an active host, as if the member's advert had been heard.
void hostlink_note_name(const uint8_t* pubkey, const char* name);
