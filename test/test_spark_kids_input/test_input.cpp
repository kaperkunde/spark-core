#include <gtest/gtest.h>
#include <vector>
#include "../../examples/spark_kids/KidsInput.cpp"

using namespace kids;

struct Rec {
  Gesture g;
  std::vector<UiEvent> ev;
  void feed(uint8_t p, int x, int y, uint32_t t) {
    UiEvent o[2];
    int n = g.feed(p, x, y, t, o);
    for (int i = 0; i < n; i++) ev.push_back(o[i]);
  }
  void tick(uint32_t t) {
    UiEvent o[2];
    int n = g.tick(t, o);
    for (int i = 0; i < n; i++) ev.push_back(o[i]);
  }
  int count(UiType t) const { int c = 0; for (auto& e : ev) c += e.type == t; return c; }
  const UiEvent* last(UiType t) const { const UiEvent* r = nullptr; for (auto& e : ev) if (e.type == t) r = &e; return r; }
  int scrolled() const { int s = 0; for (auto& e : ev) if (e.type == UI_SCROLL) s += e.dy; return s; }
};

TEST(Gesture, TapOnRelease) {
  Rec r;
  r.feed(1, 100, 120, 1000);
  ASSERT_EQ(r.count(UI_PRESS), 1);
  EXPECT_EQ(r.count(UI_TAP), 0);   // nothing until release
  r.feed(1, 104, 123, 1020);       // wobble within slop
  r.feed(0, 0, 0, 1100);
  ASSERT_EQ(r.count(UI_TAP), 1);
  EXPECT_EQ(r.last(UI_TAP)->x, 100);
  EXPECT_EQ(r.last(UI_TAP)->y, 120);
}

TEST(Gesture, SlopCancelsTapAndScrolls) {
  Rec r;
  r.feed(1, 100, 100, 0);
  r.feed(1, 101, 115, 20);
  r.feed(1, 102, 140, 40);
  r.feed(0, 0, 0, 2000);
  EXPECT_EQ(r.count(UI_TAP), 0);
  EXPECT_EQ(r.count(UI_CANCEL), 1);
  EXPECT_EQ(r.scrolled(), 40);
}

TEST(Gesture, LongPressIsNotATap) {
  Rec r;
  r.feed(1, 50, 50, 0);
  r.feed(1, 50, 50, 700);
  r.feed(0, 0, 0, 720);
  EXPECT_EQ(r.count(UI_TAP), 0);
  EXPECT_EQ(r.count(UI_CANCEL), 1);
}

TEST(Gesture, LeftSwipeIsBack) {
  Rec r;
  r.feed(1, 250, 120, 0);
  for (int i = 1; i <= 10; i++) r.feed(1, 250 - i * 10, 120 + i, i * 20);
  r.feed(0, 0, 0, 240);
  EXPECT_EQ(r.count(UI_BACK), 1);
  EXPECT_EQ(r.count(UI_TAP), 0);
  EXPECT_EQ(r.scrolled(), 0);
  // the hint reached 100 % before release
  int maxp = 0;
  for (auto& e : r.ev) if (e.type == UI_BACK_HINT && e.progress > maxp) maxp = e.progress;
  EXPECT_EQ(maxp, 100);
}

TEST(Gesture, SlowLongSwipeStillBack_SlowShortNot) {
  Rec r;
  r.feed(1, 300, 100, 0);
  for (int i = 1; i <= 16; i++) r.feed(1, 300 - i * 10, 100, i * 100);   // 160 px in 1.6 s
  r.feed(0, 0, 0, 1700);
  EXPECT_EQ(r.count(UI_BACK), 1);
  Rec s;
  s.feed(1, 300, 100, 0);
  for (int i = 1; i <= 7; i++) s.feed(1, 300 - i * 10, 100, i * 200);    // 70 px in 1.4 s
  s.feed(0, 0, 0, 1500);
  EXPECT_EQ(s.count(UI_BACK), 0);
}

TEST(Gesture, RightSwipeAndDiagonalDoNothing) {
  Rec r;
  r.feed(1, 50, 100, 0);
  for (int i = 1; i <= 10; i++) r.feed(1, 50 + i * 15, 100, i * 20);
  r.feed(0, 0, 0, 240);
  EXPECT_EQ(r.count(UI_BACK), 0);
  EXPECT_EQ(r.count(UI_TAP), 0);
  Rec d;   // 45 degrees: locks vertical (ties scroll), never back
  d.feed(1, 250, 50, 0);
  for (int i = 1; i <= 8; i++) d.feed(1, 250 - i * 12, 50 + i * 12, i * 20);
  d.feed(0, 0, 0, 200);
  EXPECT_EQ(d.count(UI_BACK), 0);
  EXPECT_GT(d.scrolled(), 0);
}

TEST(Gesture, MultiTouchCancelsUntilAllUp) {
  Rec r;
  r.feed(1, 100, 100, 0);
  r.feed(2, 100, 100, 20);
  r.feed(1, 100, 100, 40);
  r.feed(0, 0, 0, 60);
  EXPECT_EQ(r.count(UI_TAP), 0);
  EXPECT_EQ(r.count(UI_CANCEL), 1);
  r.feed(1, 10, 10, 400);
  r.feed(0, 0, 0, 450);
  EXPECT_EQ(r.count(UI_TAP), 1);   // the next touch works again
}

TEST(Gesture, ReleaseBySilence) {
  Rec r;
  r.feed(1, 80, 80, 1000);
  r.tick(1050);
  EXPECT_EQ(r.count(UI_TAP), 0);
  r.tick(1101);
  EXPECT_EQ(r.count(UI_TAP), 1);
  r.tick(1300);
  EXPECT_EQ(r.count(UI_TAP), 1);   // only once
}

TEST(Gesture, TapRepeatGuard) {
  Rec r;
  r.feed(1, 80, 80, 1000);
  r.feed(0, 0, 0, 1050);
  r.feed(1, 80, 80, 1100);
  r.feed(0, 0, 0, 1150);   // 100 ms after the previous release: chatter
  EXPECT_EQ(r.count(UI_TAP), 1);
}

TEST(Gesture, FlingCoastsAndStops) {
  Rec r;
  r.feed(1, 100, 50, 0);
  for (int i = 1; i <= 6; i++) r.feed(1, 100, 50 + i * 20, i * 20);   // 1 px/ms
  r.feed(0, 0, 0, 130);
  int during = r.scrolled();
  uint32_t t = 130;
  for (int i = 0; i < 200; i++) { t += 20; r.tick(t); }
  int coast = r.scrolled() - during;
  EXPECT_GT(coast, 100);
  EXPECT_LT(coast, 400);
  EXPECT_FALSE(r.g.active());
}

TEST(Gesture, SwallowIgnoresRestOfTouch) {
  Rec r;
  r.feed(1, 80, 80, 0);
  r.g.swallow();
  r.feed(1, 80, 80, 20);
  r.feed(0, 0, 0, 40);
  EXPECT_EQ(r.count(UI_TAP), 0);
}

TEST(TouchCal, MapAndSolve) {
  TouchCal c = { 0, 224, 2, 314 };
  int16_t x, y;
  touchMap(c, 224, 2, x, y);
  EXPECT_EQ(x, 0); EXPECT_EQ(y, 0);
  touchMap(c, 0, 314, x, y);
  EXPECT_EQ(x, 319); EXPECT_EQ(y, 239);
  touchMap(c, 300, -20, x, y);   // clamped
  EXPECT_EQ(x, 0); EXPECT_EQ(y, 0);
  // solve from two taps generated with a known calibration
  TouchCal truth = { 5, 230, 10, 300 };
  // invert: raw_y = y0 + sx*(y1-y0)/319, raw_x = x1 - sy*(x1-x0)/239
  auto ry = [&](int sx) { return (int16_t)(truth.y0 + sx * (truth.y1 - truth.y0) / 319); };
  auto rx = [&](int sy) { return (int16_t)(truth.x1 - sy * (truth.x1 - truth.x0) / 239); };
  TouchCal got;
  ASSERT_TRUE(touchSolve(24, 24, rx(24), ry(24), 296, 216, rx(216), ry(296), got));
  EXPECT_NEAR(got.x0, truth.x0, 2); EXPECT_NEAR(got.x1, truth.x1, 2);
  EXPECT_NEAR(got.y0, truth.y0, 2); EXPECT_NEAR(got.y1, truth.y1, 2);
  EXPECT_FALSE(touchSolve(24, 24, 100, 100, 296, 216, 100, 50, got));   // swapped axes
}

int main(int argc, char** argv) {
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
