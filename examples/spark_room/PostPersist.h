#pragma once

// Glue between the room server's post queue (MyMesh::posts[]) and PostStore.
// Compiled only with ROOM_PERSIST_POSTS. MyMesh calls these from three hooks.

#include "PostStore.h"

class MyMesh;

class PostPersist {
public:
  // After prefs are loaded: mount the post volume, refill the queue, apply the clock floor.
  static void restore(MyMesh& mesh);
  // A post was stored: schedule a debounced flush.
  static void changed();
  // Call from MyMesh::loop().
  static void loop(MyMesh& mesh);

  static bool isOn();
  static void usage(uint32_t& used_kb, uint32_t& total_kb);
  static int  count(MyMesh& mesh);   // non-empty entries in the queue
  // Drop every stored post: empty the queue and erase + format the post volume ("posts reset",
  // USB only; e.g. after a bootloader update, which uses part of this region as scratch space).
  static bool reset(MyMesh& mesh);
};
