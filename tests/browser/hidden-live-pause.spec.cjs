// A Live page hidden for 2 minutes gives its streams back, keeps each tile's last picture, and takes them again when shown (#533).
// A shared view an administrator marks "Keep streaming when hidden" is left alone. The page's clock is moved on, not waited for.
const { test, expect, seedFixtures, signInNext, trackLiveSockets } = require("./helpers.cjs");

const GRACE_MS = 2 * 60_000;

function tile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}:`) });
}

async function addCameras(page, count) {
  for (let index = 1; index <= count; index += 1) {
    await page.getByRole("button", { name: new RegExp(`^Camera ${String(index).padStart(3, "0")}\\b`) }).click();
  }
}

function setHidden(page, hidden) {
  return page.evaluate(value => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (value ? "hidden" : "visible") });
    document.dispatchEvent(new Event("visibilitychange"));
  }, hidden);
}

async function bothLive(page, sockets) {
  for (const name of ["Camera 001", "Camera 002"]) await expect(tile(page, name)).toHaveAttribute("data-phase", "live", { timeout: 25_000 });
  await expect.poll(() => sockets.open()).toBe(2);
}

test("hidden for 2 minutes, the tiles stop, keep their picture and say so, and play again when the page is shown", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(2);
  await page.clock.install();
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await addCameras(page, 2);
  await bothLive(page, sockets);

  // A short absence changes nothing
  await setHidden(page, true);
  await page.clock.fastForward(GRACE_MS - 5_000);
  await page.waitForTimeout(2_000);
  await expect(tile(page, "Camera 001")).not.toHaveAttribute("data-phase", "paused");
  await expect.poll(() => sockets.open()).toBe(2);

  // Past 2 minutes: every stream is given back, and nothing on a tile looks live
  await page.clock.fastForward(10_000);
  await expect.poll(() => sockets.open()).toBe(0);
  for (const name of ["Camera 001", "Camera 002"]) {
    const paused = tile(page, name);
    await expect(paused).toHaveAttribute("data-phase", "paused");
    // The notice, and the time of the last frame, which stays put: a paused tile shows no moving clock
    await expect(paused.getByRole("status")).toContainText("Paused while this tab was hidden");
    await expect(paused.locator(".tile-state")).not.toContainText("Live");
    await expect(paused.locator("video.tile-video")).toHaveCount(1); // Its last picture stays
  }
  const opened = sockets.opened();
  const overlay = tile(page, "Camera 001").getByRole("status");
  const before = await overlay.textContent();
  await page.clock.fastForward(60_000);
  await page.waitForTimeout(2_000);
  expect(sockets.opened()).toBe(opened); // Nothing reconnects while hidden
  expect(await overlay.textContent()).toBe(before); // And nothing on the tile moves

  await setHidden(page, false);
  await bothLive(page, sockets);
  await expect(page.getByText("Paused while this tab was hidden")).toHaveCount(0);
});

test("a shared view marked to keep streaming by an administrator keeps streaming while hidden", async ({ page }) => {
  test.setTimeout(120_000);
  const fixtures = seedFixtures(2);
  await page.clock.install();
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  const ids = fixtures.camera_ids.slice(0, 2);
  const saved = await page.request.put("/api/live/shared-layouts/Control%20room", { data: { camera_ids: ids, grid_size: 4, tile_camera_ids: [...ids, null, null], layout_mode: "auto" } });
  expect(saved.status()).toBe(200);
  await page.reload();
  await page.getByRole("tab", { name: "Views" }).click();
  await page.getByRole("button", { name: /^Control room\b/ }).click();
  const keep = page.getByRole("checkbox", { name: "Keep streaming when hidden" });
  await expect(keep).not.toBeChecked(); // Off by default
  await keep.click(); // Shown checked once the server has it
  await expect(keep).toBeChecked();
  const views = await (await page.request.get("/api/live/views")).json();
  expect(views.shared_views.find(view => view.name === "Control room").keep_streaming).toBe(true);
  await bothLive(page, sockets);

  await setHidden(page, true);
  await page.clock.fastForward(GRACE_MS + 30_000);
  await page.waitForTimeout(3_000);
  await expect(page.getByText("Paused while this tab was hidden")).toHaveCount(0);
  await expect.poll(() => sockets.open()).toBe(2);
});
