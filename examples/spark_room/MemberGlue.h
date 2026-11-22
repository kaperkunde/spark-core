#pragma once

// Glue between the room server (MyMesh) and the member directory (MemberDir).
// Compiled only with ROOM_MEMBER_DIR. MyMesh calls these from its hooks.

#include <stdint.h>
#include <stddef.h>

class MyMesh;
struct ClientInfo;
namespace mesh { class Identity; }

// begin(): load /members
void member_dir_begin(MyMesh& mesh);
// a client logged in (any password): remember room/admin members
void member_dir_login(MyMesh& mesh, ClientInfo* client);
// handleRequest(): GET_MEMBERS / SET_LOCATION. Writes the reply after the tag into `out`
// and returns its length, or 0 if the request is not ours / not allowed.
int member_dir_request(MyMesh& mesh, ClientInfo* sender, const uint8_t* payload, size_t len, uint8_t* out,
                       size_t cap);
// handleCommand(): members / member del|name / where. True if handled.
bool member_dir_command(MyMesh& mesh, const char* cmd, char* reply);
// an advert was heard: refresh a known member's name
void member_dir_advert(MyMesh& mesh, const mesh::Identity& id, const uint8_t* app_data, size_t app_data_len);
// loop(): debounced save
void member_dir_loop(MyMesh& mesh);
