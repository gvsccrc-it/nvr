// How often the interface asks the server for things in the background (#536): the conditional intervals.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const polling = () => import(path.join(__dirname, "..", "web", "src", "api", "polling.ts"));
const state = (status, fetchFailureCount = 0) => ({ state: { status, fetchFailureCount } });

test("a poll keeps its normal interval while answers come", async () => {
  const { pollEvery } = await polling();
  assert.equal(pollEvery(15_000)(state("success")), 15_000);
  assert.equal(pollEvery(5_000)(state("pending")), 5_000);
});

test("a failing poll never asks faster than normal, slows with each failure, and reaches at least 30 s for a 2 s poll", async () => {
  const { failingDelay, pollEvery, MAX_FAILING_MS } = await polling();
  for (const normal of [2_000, 5_000, 15_000]) {
    let previousLow = 0;
    for (let failures = 1; failures <= 8; failures += 1) {
      const low = failingDelay(normal, failures, () => 0);
      const high = failingDelay(normal, failures, () => 1);
      assert.ok(low >= normal * 2, `${normal} ms, ${failures} failures: ${low}`);
      assert.ok(high <= Math.max(MAX_FAILING_MS, normal) * 1.25, `${normal} ms, ${failures} failures: ${high}`);
      assert.ok(low >= previousLow, "it got faster with more failures");
      previousLow = low;
    }
  }
  // The pop-in poll (every 2 s) after repeated failures: at least 30 s
  assert.ok(failingDelay(2_000, 5, () => 0) >= 30_000);
  // React Query's view of a failing poll: an error, or failures counted during the fetch
  assert.ok(pollEvery(5_000)(state("error", 2)) >= 10_000);
  assert.ok(pollEvery(5_000)(state("success", 1)) >= 10_000);
});

test("tabs do not all retry at the same instant", async () => {
  const { failingDelay, retryDelay } = await polling();
  const waits = new Set(Array.from({ length: 50 }, () => failingDelay(15_000, 2)));
  assert.ok(waits.size > 20, "every tab would wait the same");
  const retries = Array.from({ length: 50 }, () => retryDelay(0));
  assert.ok(new Set(retries).size > 20);
  assert.ok(Math.min(...retries) >= 500 && Math.max(...retries) <= 1_500);
});

test("a feature that is switched off is looked at again every 5 minutes", async () => {
  const { OFF_RECHECK_MS } = await polling();
  assert.equal(OFF_RECHECK_MS, 300_000);
});
