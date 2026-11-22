#pragma once

// Remembers which room posts were created through HostLink (SEND_TXT_MSG to the room's own
// key prefix) so they are never offered back to the host, neither as a live push nor in the
// APP_START replay. Keyed by post timestamp (unique per post). Persisted with the post
// (PostStore flags) so the rule also holds after a reboot.

#include <stdint.h>

namespace spark {

class HostPosts {
public:
  static const int N = 32;   // >= MAX_UNSYNCED_POSTS
  static void mark(uint32_t ts) { S& s = st(); s.ts[s.head] = ts; s.head = (s.head + 1) % N; }
  static bool has(uint32_t ts) {
    S& s = st();
    for (int i = 0; i < N; i++) if (s.ts[i] == ts) return true;
    return false;
  }
private:
  struct S { uint32_t ts[N]; int head; };
  static S& st() { static S s = { {0}, 0 }; return s; }
};

}  // namespace spark
