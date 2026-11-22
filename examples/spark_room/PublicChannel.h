#pragma once
// The well-known MeshCore "Public" channel, initialised the way BaseChatMesh::addChannel() does:
// secret zero-padded to the full secret buffer, hash = first bytes of SHA256 over the 16 key
// bytes. Header-only and templated on the channel type / hash function so it unit-tests natively.
#include <stdint.h>
#include <string.h>

namespace spark {

static const uint8_t PUBLIC_CHANNEL_SECRET[16] = { 0x8b, 0x33, 0x87, 0xe9, 0xc5, 0xcd, 0xea, 0x6a,
                                                    0xc9, 0xe5, 0xed, 0xba, 0xa1, 0x15, 0xcd, 0x72 };

// Sha: void(uint8_t* hash, size_t hash_len, const uint8_t* msg, int msg_len), e.g. mesh::Utils::sha256
template <class Channel, class Sha>
inline void initPublicChannel(Channel& ch, Sha sha256) {
  memset(ch.secret, 0, sizeof(ch.secret));
  memcpy(ch.secret, PUBLIC_CHANNEL_SECRET, sizeof(PUBLIC_CHANNEL_SECRET));
  sha256(ch.hash, sizeof(ch.hash), PUBLIC_CHANNEL_SECRET, (int)sizeof(PUBLIC_CHANNEL_SECRET));
}

// Does a received GRP_TXT/GRP_DATA channel-hash byte select this channel?
template <class Channel>
inline bool channelHashMatches(const Channel& ch, const uint8_t* hash) { return hash[0] == ch.hash[0]; }

}  // namespace spark
