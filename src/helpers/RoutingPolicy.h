#pragma once

#include <Packet.h>
#include <string.h>

namespace mesh {

/**
 * \brief  Test a flood packet against the configured hop limits.
 * \param  packet  inbound flood packet (caller has already checked isRouteFlood())
 * \param  flood_max            max hops for any flood packet
 * \param  flood_max_unscoped   max hops for ROUTE_TYPE_FLOOD (ie. un-scoped) packets
 * \param  flood_max_advert     max hops for ADVERT packets
 * \returns  true if the packet has exceeded a limit, and must not be forwarded
 */
inline bool isFloodHopLimitExceeded(const Packet* packet, uint8_t flood_max,
                                    uint8_t flood_max_unscoped, uint8_t flood_max_advert) {
  uint8_t hops = packet->getPathHashCount();
  if (hops >= flood_max) return true;
  if (packet->getRouteType() == ROUTE_TYPE_FLOOD && hops >= flood_max_unscoped) return true;
  if (packet->getPayloadType() == PAYLOAD_TYPE_ADVERT && hops >= flood_max_advert) return true;
  return false;
}

/**
 * \brief  Flood loop detection, as the repeater does it: count how often this node's own hash
 *         already appears in the packet's path, against a maximum per path hash size.
 * \param  packet        inbound flood packet (caller has already checked isRouteFlood())
 * \param  self_pub_key  this node's public key (its first bytes are the path hash)
 * \param  loop_detect   0 off, 1 minimal, 2 moderate, 3 strict (CommonCLI LOOP_DETECT_*)
 * \returns  true if the packet has looped back here too often, and must not be forwarded
 */
inline bool isFloodLooped(const Packet* packet, const uint8_t* self_pub_key, uint8_t loop_detect) {
  //                                         1-byte  2-byte  3-byte hashes
  static const uint8_t max_loop_minimal[]  = { 0,  4,      2,      1 };
  static const uint8_t max_loop_moderate[] = { 0,  2,      1,      1 };
  static const uint8_t max_loop_strict[]   = { 0,  1,      1,      1 };
  if (loop_detect == 0) return false;
  const uint8_t* maximums = loop_detect == 1 ? max_loop_minimal
                          : loop_detect == 2 ? max_loop_moderate : max_loop_strict;
  uint8_t hash_size = packet->getPathHashSize();
  if (hash_size > 3) return false;   // reserved path hash mode
  uint8_t hash_count = packet->getPathHashCount();
  uint8_t n = 0;
  const uint8_t* path = packet->path;
  while (hash_count > 0) {
    if (memcmp(path, self_pub_key, hash_size) == 0) n++;
    hash_count--;
    path += hash_size;
  }
  return n >= maximums[hash_size];
}

/**
 * \brief  How a server routes a reply back to the requesting client.
 */
enum ReplyRoute : uint8_t {
  REPLY_ROUTE_PATH_RETURN,       // request arrived by flood: reply with a PATH return, flooded back
  REPLY_ROUTE_DIRECT_SUPPLIED,   // reply DIRECT, along the return path supplied in the request
  REPLY_ROUTE_DIRECT_OUT_PATH,   // reply DIRECT, along the out_path already stored for this client
  REPLY_ROUTE_FLOOD,             // no return path known: flood the reply
};

/**
 * \param  inbound_is_flood    the request arrived as a flood packet
 * \param  have_supplied_path  the request payload carried an explicit reply path
 * \param  have_out_path       this server already has a stored out_path for the client
 */
inline ReplyRoute chooseReplyRoute(bool inbound_is_flood, bool have_supplied_path, bool have_out_path) {
  if (inbound_is_flood) return REPLY_ROUTE_PATH_RETURN;
  if (have_supplied_path) return REPLY_ROUTE_DIRECT_SUPPLIED;
  if (have_out_path) return REPLY_ROUTE_DIRECT_OUT_PATH;
  return REPLY_ROUTE_FLOOD;
}

/**
 * \brief  Which transport scope a flooded reply should be sent with.
 */
enum ReplyScope : uint8_t {
  REPLY_SCOPE_REQUEST,   // re-use the scope the request arrived on
  REPLY_SCOPE_DEFAULT,   // fall back to this node's default region scope
  REPLY_SCOPE_NONE,      // send un-scoped (ROUTE_TYPE_FLOOD)
};

/**
 * \param  request_scope_known         request arrived scoped, and we resolved its Region's key
 * \param  request_was_unscoped_flood  request arrived as an un-scoped flood
 * \param  default_scope_known         this node has a default Region with a usable transport key
 */
inline ReplyScope chooseReplyScope(bool request_scope_known, bool request_was_unscoped_flood,
                                   bool default_scope_known) {
  if (request_scope_known) return REPLY_SCOPE_REQUEST;
  if (request_was_unscoped_flood) return REPLY_SCOPE_NONE;  // requester chose un-scoped, so mirror it
  if (default_scope_known) return REPLY_SCOPE_DEFAULT;      // scope unknowable: DIRECT, or unresolved Region
  return REPLY_SCOPE_NONE;
}

}
