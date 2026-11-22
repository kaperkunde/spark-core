#include "PostPersist.h"

#ifdef ROOM_PERSIST_POSTS

#include "../simple_room_server/MyMesh.h"
#include <CustomLFS.h>
#include "HostPosts.h"

// The 100 KB "extrafs" region 0xD4000..0xED000 sits directly below InternalFS
// (0xED000, 28 KB: identity, prefs, ACL), which is never touched here.
static CustomLFS ExtraFS(0xD4000, 0x19000, 128);
static spark::PostStore store;

namespace {
struct LoadCtx { MyMesh* mesh; int n; };
}  // namespace

// Friend access to MyMesh internals goes through this class.
class PostPersistAccess {
public:
  // MyMesh::posts is a cyclic queue; entry k (0 = oldest) lives at (next_post_idx + k) % N,
  // empty slots have post_timestamp == 0.
  static int nth(MyMesh& m, int k) {
    for (int j = 0, seen = 0; j < MAX_UNSYNCED_POSTS; j++) {
      int idx = (m.next_post_idx + j) % MAX_UNSYNCED_POSTS;
      if (m.posts[idx].post_timestamp == 0) continue;
      if (seen++ == k) return idx;
    }
    return -1;
  }
  static bool get(void* ctx, int i, spark::PostRec& out) {
    MyMesh* m = (MyMesh*)ctx;
    int idx = nth(*m, i);
    if (idx < 0) return false;
    PostInfo& p = m->posts[idx];
    out.timestamp = p.post_timestamp;
    memcpy(out.author, p.author.pub_key, 32);
    size_t tl = strlen(p.text);
    if (tl > spark::POST_MAX_TEXT) tl = spark::POST_MAX_TEXT;
    out.text_len = (uint8_t)tl;
    memcpy(out.text, p.text, tl);
    out.text[tl] = 0;
    out.flags = spark::HostPosts::has(p.post_timestamp) ? spark::POST_FLAG_HOST : 0;
    return true;
  }
  static void put(void* ctx, const spark::PostRec& r) {
    LoadCtx* c = (LoadCtx*)ctx;
    if (c->n >= MAX_UNSYNCED_POSTS) return;
    PostInfo& p = c->mesh->posts[c->n++];
    p.post_timestamp = r.timestamp;
    if (r.flags & spark::POST_FLAG_HOST) spark::HostPosts::mark(r.timestamp);
    memcpy(p.author.pub_key, r.author, 32);
    memcpy(p.text, r.text, r.text_len);
    p.text[r.text_len] = 0;
  }
  static int count(MyMesh& m) {
    int n = 0;
    for (int i = 0; i < MAX_UNSYNCED_POSTS; i++) if (m.posts[i].post_timestamp != 0) n++;
    return n;
  }
  static void finishRestore(MyMesh& m, int n, uint32_t last_known) {
    m.next_post_idx = n % MAX_UNSYNCED_POSTS;
    // The room has no battery-backed clock: after a reboot it restarts in May 2024, so new
    // posts would be older than the persisted ones and clients would never be sent them.
    // Floor the clock at max(last known time, newest persisted post + 1).
    uint32_t floor_t = last_known;
    for (int i = 0; i < n; i++) {
      if (m.posts[i].post_timestamp + 1 > floor_t) floor_t = m.posts[i].post_timestamp + 1;
    }
    raiseClock(m, floor_t);
  }
  static void raiseClock(MyMesh& m, uint32_t t) {
    if (t > m.getRTCClock()->getCurrentTime()) m.getRTCClock()->setCurrentTime(t);
  }
  static void flush(MyMesh& m) {
    store.loop(millis(), count(m), get, &m, m.getRTCClock()->getCurrentTime());
  }
};

void PostPersist::restore(MyMesh& mesh) {
  // Even with no post file (fresh flash) the clock must not start in May 2024: use the
  // firmware build date as a lower bound. The host sets the real time on connect.
  PostPersistAccess::raiseClock(mesh, spark::epochFromCDate(__DATE__));
  if (!ExtraFS.begin()) {
    store.begin(NULL);   // RAM only
    return;
  }
  store.begin(&ExtraFS);
  LoadCtx ctx = { &mesh, 0 };
  uint32_t last_known = 0;
  int n = store.load(PostPersistAccess::put, &ctx, last_known);
  if (n < 0) return;
  PostPersistAccess::finishRestore(mesh, ctx.n, last_known);
}

void PostPersist::changed() { store.markDirty(millis()); }
void PostPersist::loop(MyMesh& mesh) { PostPersistAccess::flush(mesh); }
bool PostPersist::isOn() { return store.isOn(); }
void PostPersist::usage(uint32_t& used_kb, uint32_t& total_kb) { store.used(used_kb, total_kb); }
int PostPersist::count(MyMesh& mesh) { return PostPersistAccess::count(mesh); }

#endif  // ROOM_PERSIST_POSTS
