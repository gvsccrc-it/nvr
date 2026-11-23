// One open tab makes far fewer background requests (#536): counted in a real browser over a minute.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

function counter(page) {
  const seen = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) seen.push({ at: Date.now(), path: url.pathname, query: url.search });
  });
  return seen;
}

test("an idle tab with the console and failover off makes at most about 26 requests a minute", async ({ page }) => {
  test.setTimeout(150_000);
  seedFixtures(4);
  await signInNext(page);
  const seen = counter(page);
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(5_000); // The first reads of every panel are over; what follows is the background
  const from = Date.now();
  await page.waitForTimeout(60_000);
  const minute = seen.filter(entry => entry.at >= from);
  const byPath = minute.reduce((counts, entry) => ({ ...counts, [entry.path]: (counts[entry.path] ?? 0) + 1 }), {});
  console.log(`requests in one idle minute: ${minute.length} ${JSON.stringify(byPath)}`);
  expect(minute.length, JSON.stringify(byPath)).toBeLessThanOrEqual(27);
  // Switched off: looked at again every 5 minutes, not every 10 or 15 seconds
  expect(byPath["/api/console/status"] ?? 0).toBe(0);
  expect(byPath["/api/failover/status"] ?? 0).toBe(0);
  // Incidents in one request, and the alarm chip by its counts alone
  expect(minute.filter(entry => entry.path === "/api/incidents" && entry.query !== "?state=open")).toEqual([]);
  expect(minute.filter(entry => entry.path === "/api/alarms" && !entry.query.includes("limit=0"))).toEqual([]);
});

test("the alarm pop-in poll stops while the page is hidden and starts again when it is shown", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page, "camera-operator");
  const popIn = page.getByRole("button", { name: /Alarm pop-in/ });
  if ((await popIn.getAttribute("aria-pressed")) !== "true") await popIn.click();
  await expect(popIn).toHaveAttribute("aria-pressed", "true");
  const seen = counter(page);
  const polls = () => seen.filter(entry => entry.path === "/api/live/alarm-pop-ins").length;
  await expect.poll(polls, { timeout: 10_000 }).toBeGreaterThan(1);

  // Hidden: as the browser reports a tab in the background
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(2_500); // A poll already on its way may still land
  const whileHidden = polls();
  await page.waitForTimeout(6_000);
  expect(polls()).toBe(whileHidden);

  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(polls, { timeout: 5_000 }).toBeGreaterThan(whileHidden);
});
