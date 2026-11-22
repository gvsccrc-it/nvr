// A video wall nobody is standing at recovers by itself (#535): it waits for an NVR that cannot be reached and does not ask for a
// token, it fetches its views again, and it reloads when the server gives out a newer interface. The API is played by the test, so
// each failure happens exactly when the journey says.
const { test, expect, seedFixtures } = require("./helpers.cjs");

const view = name => ({ name, scope: "shared", camera_ids: [], grid_size: 4, tile_camera_ids: [null, null, null, null], layout_mode: "auto" });
const json = body => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
const down = { status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Starting" }) };

/** Play the wall's API. `nvr` is changed by the test as it goes: what the session and the views answer next. */
async function play(page, nvr) {
  await page.route("**/api/wall/session", route => {
    nvr.sessionAsked += 1;
    if (nvr.session === "unreachable") return route.abort("connectionrefused");
    if (nvr.session === "starting") return route.fulfill(down);
    if (nvr.session === "signed-out") return route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Sign the video wall in with an API token" }) });
    return route.fulfill(json({ name: "Lobby wall", scopes: ["read", "media"], interface: nvr.interface }));
  });
  await page.route("**/api/live/views", route => (nvr.views ? route.fulfill(json({ layouts: [], shared_views: nvr.views.map(view) })) : route.fulfill(down)));
  await page.route("**/api/cameras", route => route.fulfill(json({ cameras: [] })));
  await page.route("**/api/settings/display", route => route.fulfill(json({ timezone: "UTC", hour_cycle: "24" })));
  await page.route("**/api/live/alarm-pop-ins**", route => route.fulfill(json({ alarms: [], cursor: "0" })));
}

/**
 * Move the page's clock on, a step at a time, until `done()` holds. One step is not always enough. The wall's every-minute check is a
 * timer that the query library starts again whenever a check begins or ends. A test that waits only for a request to reach the stand-in
 * can move the clock while that answer is still on its way back: the check then due is merged into the one in flight (answered from
 * before the change), and the timer starts again a minute after the clock already is. A CI run waited for a reload that never came;
 * with each answer held back 1.5 s the single step fails every time, and this passes.
 */
async function until(page, done, step) {
  await expect.poll(async () => {
    if (await done()) return true;
    await page.clock.fastForward(step);
    return done();
  }, { timeout: 20_000 }).toBe(true);
}

const fresh = () => ({ session: "ok", sessionAsked: 0, interface: "build-one", views: ["Lobby"] });

test("a wall that starts before the NVR waits for it and enters by itself; only a refused session shows the token form", async ({ page }) => {
  seedFixtures(1);
  await page.clock.install();
  const nvr = { ...fresh(), session: "unreachable" };
  await play(page, nvr);
  await page.goto("/wall?view=Lobby&view=Yard&dwell=30");

  const waiting = page.getByRole("status").filter({ hasText: "Waiting for the NVR" });
  await expect(waiting).toBeVisible();
  await expect(page.getByRole("form", { name: "Video wall sign-in" })).toHaveCount(0);
  // Still not there, now answering 503 while it starts: the wall keeps asking, and still does not ask for a token.
  nvr.session = "starting";
  const asked = nvr.sessionAsked;
  await until(page, () => nvr.sessionAsked > asked, 20_000);
  await expect(waiting).toBeVisible();
  await expect(page.getByRole("form", { name: "Video wall sign-in" })).toHaveCount(0);

  // The NVR answers: the wall is entered with nobody touching it.
  nvr.session = "ok";
  const screen = page.getByRole("main", { name: "Video wall" });
  await until(page, () => screen.isVisible(), 31_000);
  await expect(screen).toContainText("Lobby");
  await expect(screen).not.toContainText("of 2");

  // A shared view an administrator adds reaches the running wall within the background interval.
  nvr.views = ["Lobby", "Yard"];
  await until(page, async () => ((await screen.textContent()) || "").includes("Lobby · 1 of 2"), 61_000);

  // A session the NVR refuses (the token revoked, or its 30 days over) is the one case that shows the form.
  nvr.session = "signed-out";
  await until(page, () => page.getByRole("form", { name: "Video wall sign-in" }).isVisible(), 61_000);
});

test("a wall whose first fetch of its views fails draws them once the NVR answers, with no reload", async ({ page }) => {
  seedFixtures(1);
  await page.clock.install();
  const nvr = { ...fresh(), views: null };
  await play(page, nvr);
  let loads = 0;
  page.on("load", () => { loads += 1; });
  await page.goto("/wall?view=Lobby&dwell=30");
  await expect(page.getByRole("status").filter({ hasText: "Waiting for the NVR" })).toBeVisible();
  nvr.views = ["Lobby"];
  await until(page, () => page.getByRole("main", { name: "Video wall" }).isVisible(), 6_000);
  await expect(page.getByRole("main", { name: "Video wall" })).toContainText("Lobby");
  expect(loads).toBe(1);
});

test("a wall left running across an upgrade reloads itself once the server gives out a newer interface", async ({ page }) => {
  seedFixtures(1);
  await page.clock.install();
  const nvr = fresh();
  await play(page, nvr);
  let loads = 0;
  page.on("load", () => { loads += 1; });
  await page.goto("/wall?view=Lobby&dwell=30");
  await expect(page.getByRole("main", { name: "Video wall" })).toContainText("Lobby");
  // The same interface at the next check: nothing happens.
  await page.clock.fastForward(61_000);
  await expect.poll(() => nvr.sessionAsked).toBeGreaterThan(1);
  expect(loads).toBe(1);

  nvr.interface = "build-two";
  await until(page, () => loads === 2, 61_000);
  await expect(page.getByRole("main", { name: "Video wall" })).toContainText("Lobby");
  // Loaded with the new one, it stays put.
  await page.clock.fastForward(61_000);
  await expect(page.getByRole("main", { name: "Video wall" })).toContainText("Lobby");
  expect(loads).toBe(2);
});
