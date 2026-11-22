// The Playback timeline does not redraw every camera's marks as the clock ticks (#532).
// It read the clock at its top, so each of the ten ticks a second while playing re-rendered every mark of every camera:
// a day of 16 cameras was about 1.8 seconds of work for each second played. Only the cursor and the slider's value follow the clock.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

test("while playing, the cursor follows the clock and no camera's marks are drawn again", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(4, { footage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await signInNext(page);
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  await expect(page.getByRole("form", { name: "Footage search" })).toBeVisible();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  for (const index of [1, 2, 3, 4]) await page.getByRole("button", { name: new RegExp(`^Camera 00${index}\\b`) }).click();
  await expect(page.locator(".timeline-row[data-camera-id]")).toHaveCount(4);
  await expect(page.locator(".timeline-row[data-camera-id][data-footage='loading']")).toHaveCount(0);
  await expect(page.locator(".timeline-row[data-camera-id][data-footage='some']")).toHaveCount(2); // The fixture gives the first two cameras footage
  await expect(page.locator(".timeline-mark[data-mark='recording']").first()).toBeVisible();

  const ruler = page.getByRole("slider", { name: "Playback position" });
  const cursor = page.locator(".timeline-row[data-camera-id] .timeline-cursor").first();
  const drawn = () => page.evaluate(() => globalThis.__timelineTracksDrawn ?? 0);
  const position = async () => ({ value: Number(await ruler.getAttribute("aria-valuenow")), left: await cursor.evaluate(element => element.style.left) });

  expect(await drawn()).toBeGreaterThanOrEqual(4); // Each camera's marks were drawn when its row arrived
  await expect(ruler).toHaveAttribute("aria-valuetext", /\d/);
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect.poll(async () => (await position()).value).toBeGreaterThan((await position()).value - 1); // The slider has a value
  const before = await position();
  const drawnBefore = await drawn();
  await expect.poll(async () => (await position()).value, { timeout: 15_000 }).toBeGreaterThan(before.value + 1500); // A second and a half of playing: fifteen ticks
  const after = await position();
  expect(after.left).not.toBe(before.left); // The cursor moved
  expect(await drawn()).toBe(drawnBefore); // And nothing else was drawn again

  // A change of the visible window is a reason to draw the marks again, once.
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  const paused = await drawn();
  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect.poll(drawn).toBeGreaterThan(paused);
  const zoomed = await drawn();
  expect(zoomed - paused).toBeLessThanOrEqual(8); // Four rows, drawn once (twice at most, as the window settles)
});
