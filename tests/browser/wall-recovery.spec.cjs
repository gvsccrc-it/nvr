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
  await page.clock.fastForward(20_000);
  await expect.poll(() => nvr.sessionAsked).toBeGreaterThan(asked);
  await expect(waiting).toBeVisible();
  await expect(page.getByRole("form", { name: "Video wall sign-in" })).toHaveCount(0);

  // The NVR answers: the wall is entered with nobody touching it.
  nvr.session = "ok";
  await page.clock.fastForward(31_000);
  const screen = page.getByRole("main", { name: "Video wall" });
  await expect(screen).toContainText("Lobby");
  await expect(screen).not.toContainText("of 2");

  // A shared view an administrator adds reaches the running wall within the background interval.
  nvr.views = ["Lobby", "Yard"];
  await page.clock.fastForward(61_000);
  await expect(screen).toContainText("Lobby · 1 of 2");

  // A session the NVR refuses (the token revoked, or its 30 days over) is the one case that shows the form.
  nvr.session = "signed-out";
  await page.clock.fastForward(61_000);
  await expect(page.getByRole("form", { name: "Video wall sign-in" })).toBeVisible();
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
  await page.clock.fastForward(6_000);
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
  await page.clock.fastForward(61_000);
  await expect.poll(() => loads).toBe(2);
  await expect(page.getByRole("main", { name: "Video wall" })).toContainText("Lobby");
  // Loaded with the new one, it stays put.
  await page.clock.fastForward(61_000);
  await expect(page.getByRole("main", { name: "Video wall" })).toContainText("Lobby");
  expect(loads).toBe(2);
});
