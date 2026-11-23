// Small interface costs that grow with cameras and viewers (#539): reconnect timing, fisheye painting, camera list order.
// The modules are plain TypeScript with no imports, which Node runs directly.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const source = relative => import(path.join(__dirname, "..", "web", "src", "features", relative));

test("reconnect waits vary within their bounds, two players made together do not match, and the longest is 30 s", async () => {
  const { reconnectDelay, RECONNECT_STEPS_MS } = await source("live/reconnect.ts");
  RECONNECT_STEPS_MS.forEach((step, attempt) => {
    assert.equal(reconnectDelay(attempt, () => 0), step / 2);
    assert.equal(reconnectDelay(attempt, () => 1), step);
    const seen = new Set(Array.from({ length: 50 }, () => reconnectDelay(attempt)));
    assert.ok(seen.size > 10, `attempt ${attempt} always waited the same`);
    for (const wait of seen) assert.ok(wait >= step / 2 && wait <= step, `attempt ${attempt}: ${wait}`);
  });
  // Past the last step the wait stays between 15 s and 30 s: the longest is still about 30 s
  const late = Array.from({ length: 200 }, () => reconnectDelay(99));
  assert.ok(Math.min(...late) >= 15_000 && Math.max(...late) <= 30_000);
  assert.ok(Math.max(...late) > 28_000, "the longest wait is no longer about 30 s");
  // Many tiles reconnecting after one restart: their first retries spread over half a second, instead of all landing at +1 s
  const first = Array.from({ length: 64 }, () => reconnectDelay(0));
  assert.ok(new Set(first).size > 40);
  assert.ok(Math.max(...first) - Math.min(...first) > 300);
});

function fakeClock() {
  let next = 1;
  const animation = new Map();
  const timers = new Map();
  return {
    animation,
    timers,
    clock: {
      animationFrame: callback => { const id = next++; animation.set(id, callback); return id; },
      cancelAnimationFrame: id => animation.delete(id),
      setTimeout: (callback, ms) => { const id = next++; timers.set(id, { callback, ms }); return id; },
      clearTimeout: id => timers.delete(id),
    },
    runAnimationFrame() { const pending = [...animation.values()]; animation.clear(); pending.forEach(callback => callback()); },
    runTimers() { const pending = [...timers.values()]; timers.clear(); pending.forEach(({ callback }) => callback()); },
  };
}

function fakeVideo() {
  let next = 1;
  const waiting = new Map();
  return {
    waiting,
    requestVideoFrameCallback: callback => { const id = next++; waiting.set(id, callback); return id; },
    cancelVideoFrameCallback: id => waiting.delete(id),
    newFrame() { const pending = [...waiting.values()]; waiting.clear(); pending.forEach(callback => callback()); },
  };
}

test("a dewarped video tile paints at the camera's frame rate, not the screen's", async () => {
  const { paintFrames } = await source("dewarp/frames.ts");
  const time = fakeClock();
  const video = fakeVideo();
  let paints = 0;
  const stop = paintFrames(() => video, () => { paints += 1; }, time.clock);
  assert.equal(paints, 1); // At once
  // One second: the screen refreshes 60 times, the camera sends 15 frames
  for (let refresh = 0; refresh < 60; refresh += 1) {
    time.runAnimationFrame();
    if (refresh % 4 === 0) video.newFrame();
  }
  assert.equal(paints, 1 + 15);
  stop();
  video.newFrame();
  time.runTimers();
  assert.equal(paints, 16, "it painted after being stopped");
});

test("a picture with no frame callback (compatibility mode) still updates on every animation frame", async () => {
  const { paintFrames } = await source("dewarp/frames.ts");
  const time = fakeClock();
  const picture = {}; // An <img> or a canvas
  let paints = 0;
  const stop = paintFrames(() => picture, () => { paints += 1; }, time.clock);
  for (let refresh = 0; refresh < 10; refresh += 1) time.runAnimationFrame();
  assert.equal(paints, 11);
  stop();
  assert.equal(time.animation.size, 0);
});

test("a video that stops producing frames, or is replaced by another source, is looked at again after a short wait", async () => {
  const { paintFrames, WAKE_MS } = await source("dewarp/frames.ts");
  const time = fakeClock();
  const silent = fakeVideo();
  let current = silent;
  let paints = 0;
  const stop = paintFrames(() => current, () => { paints += 1; }, time.clock);
  assert.equal([...time.timers.values()][0].ms, WAKE_MS);
  current = {}; // The tile fell back to the compatibility picture; the old video will never call back
  time.runTimers();
  assert.equal(paints, 2);
  assert.equal(silent.waiting.size, 0, "the old video's callback was left waiting");
  time.runAnimationFrame();
  assert.equal(paints, 3);
  stop();
});

test("cameras are ordered by name with numbers in order, case ignored, then by id, and the key changes only with names", async () => {
  const { orderOf, namesKey, compareCameras } = await source("cameras/order.ts");
  const cameras = [
    { id: "c", name: "Camera 10" }, { id: "a", name: "camera 2" }, { id: "b", name: "Camera 2" }, { id: "d", name: "Dock" }, { id: "e", name: "Étage" },
  ];
  assert.deepEqual(orderOf(cameras), ["a", "b", "c", "d", "e"]);
  // The same as the comparison it replaces, on these and on a thousand generated names
  const before = (x, y) => x.name.localeCompare(y.name, undefined, { numeric: true, sensitivity: "base" }) || x.id.localeCompare(y.id);
  const many = Array.from({ length: 1000 }, (_, index) => ({ id: `id-${(index * 7919) % 1000}`, name: `${["Gate", "gate", "Dock", "Hall"][index % 4]} ${index % 37}` }));
  assert.deepEqual(orderOf(many), [...many].sort(before).map(camera => camera.id));
  assert.equal(Math.sign(compareCameras(cameras[0], cameras[1])), 1);
  // A refresh brings new objects with the same names: same key, so the order is not worked out again
  assert.equal(namesKey(many.map(camera => ({ ...camera, health: "new" }))), namesKey(many));
  assert.notEqual(namesKey([...many.slice(1), { ...many[0], name: "Renamed" }]), namesKey(many));
});

test("for 1,000 cameras, a refresh with the same names costs well under a frame, and sorting with one comparer is cheaper", async () => {
  const { orderOf, namesKey } = await source("cameras/order.ts");
  const many = Array.from({ length: 1000 }, (_, index) => ({ id: `id-${(index * 7919) % 1000}`, name: `Camera ${(index * 31) % 1000}` }));
  const best = run => Math.min(...Array.from({ length: 7 }, () => { const started = performance.now(); run(); return performance.now() - started; }));
  const refresh = best(() => namesKey(many.map(camera => ({ ...camera }))));
  const sortShared = best(() => orderOf(many));
  const sortBefore = best(() => [...many].sort((x, y) => x.name.localeCompare(y.name, undefined, { numeric: true, sensitivity: "base" }) || x.id.localeCompare(y.id)));
  console.log(`1,000 cameras: refresh with the same names ${refresh.toFixed(2)} ms; sort with one comparer ${sortShared.toFixed(2)} ms, as before ${sortBefore.toFixed(2)} ms`);
  assert.ok(refresh < 16, `${refresh} ms`);
  assert.ok(sortShared < sortBefore, `${sortShared} ms is not less than ${sortBefore} ms`);
});
