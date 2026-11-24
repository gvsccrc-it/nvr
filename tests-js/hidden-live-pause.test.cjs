// A Live page hidden for 2 minutes gives its streams back, and takes them again as soon as it is shown (#533).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const source = () => import(path.join(__dirname, "..", "web", "src", "features", "live", "hiddenPause.ts"));

function fakePage() {
  let hidden = false;
  let now = 0;
  let next = 1;
  const timers = new Map();
  const listeners = new Set();
  return {
    timers,
    listeners,
    page: {
      hidden: () => hidden,
      listen: onChange => { listeners.add(onChange); return () => listeners.delete(onChange); },
      setTimeout: (callback, ms) => { const id = next++; timers.set(id, { callback, at: now + ms }); return id; },
      clearTimeout: id => timers.delete(id),
    },
    setHidden(value) { hidden = value; listeners.forEach(listener => listener()); },
    advance(ms) {
      now += ms;
      for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.callback(); }
    },
  };
}

test("the grace period is the owner's 2 minutes", async () => {
  const { HIDDEN_GRACE_MS } = await source();
  assert.equal(HIDDEN_GRACE_MS, 120_000);
});

test("hidden for less than the grace period: nothing pauses; hidden for longer: paused, and shown again: playing at once", async () => {
  const { watchHiddenTooLong, HIDDEN_GRACE_MS } = await source();
  const fake = fakePage();
  const seen = [];
  const stop = watchHiddenTooLong(value => seen.push(value), HIDDEN_GRACE_MS, fake.page);
  assert.deepEqual(seen, []);
  // A quick look at another tab
  fake.setHidden(true);
  fake.advance(HIDDEN_GRACE_MS - 1);
  fake.setHidden(false);
  fake.advance(HIDDEN_GRACE_MS * 2);
  assert.deepEqual(seen, [], "a short absence paused the streams");
  // Hidden again: the clock starts again from nothing
  fake.setHidden(true);
  fake.advance(HIDDEN_GRACE_MS - 1);
  assert.deepEqual(seen, []);
  fake.advance(1);
  assert.deepEqual(seen, [true]);
  // More visibility events while hidden change nothing and start no second timer
  fake.setHidden(true);
  fake.advance(HIDDEN_GRACE_MS * 3);
  assert.deepEqual(seen, [true]);
  assert.equal(fake.timers.size, 0);
  fake.setHidden(false);
  assert.deepEqual(seen, [true, false]);
  stop();
  assert.equal(fake.listeners.size, 0);
});

test("a page already hidden when the watch starts pauses after the grace period, and stopping the watch leaves no timer", async () => {
  const { watchHiddenTooLong } = await source();
  const fake = fakePage();
  fake.setHidden(true);
  const seen = [];
  const stop = watchHiddenTooLong(value => seen.push(value), 1_000, fake.page);
  fake.advance(1_000);
  assert.deepEqual(seen, [true]);
  const again = fakePage();
  again.setHidden(true);
  const stopAgain = watchHiddenTooLong(() => assert.fail("called after stop"), 1_000, again.page);
  stopAgain();
  assert.equal(again.timers.size, 0);
  again.advance(5_000);
  stop();
});
