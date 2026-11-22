// A live tile never sits on a frozen picture labelled "Live" (#534). In compatibility mode (a stream of JPEG pictures, for browsers
// that cannot play the camera's video) the player reads the stream itself: it sees it end, asks again by itself, and judges freshness
// from its own pictures. In either mode a connection that gives nothing is dropped and asked for again. The test plays the server's
// side of the live connection, so each failure happens exactly when the journey says.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const JPEG = Buffer.from("/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAgoKC8oLzc3Nzc3N0E8QUNDQ0FBQUFDQ0NISEhVVVVISEhDQ0hIUFBVVVxfXFdXVVdfX2RkZHh4c3OMjJGsrM//xABMAAEBAAAAAAAAAAAAAAAAAAAABgEBAQAAAAAAAAAAAAAAAAAABQYQAQAAAAAAAAAAAAAAAAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAQABADASIAAhEAAxEA/9oADAMBAAIRAxEAPwCSAVYB/9k=", "base64");
const PART = [...Buffer.concat([Buffer.from(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${JPEG.length}\r\n\r\n`), JPEG, Buffer.from("\r\n")])];

/**
 * Play the NVR's side of live video for a browser that cannot play the camera's video.
 *
 * The socket answers "unsupported" and names the picture stream, as the server does; with `nvr.video` set it stays open and sends
 * nothing instead. The picture stream is played inside the page, because it has to stay open and be ended at a chosen moment:
 * `window.pictureStream.mode` is "pictures" (one every 200 ms, each in two pieces), "nothing" (it ends without one) or "silent" (it
 * never answers), and `end` ends the stream that is open cleanly, as the server does when masks or access change. The camera-wide
 * status always says "live", as it does while anyone else is watching the camera.
 */
async function play(page, nvr) {
  await page.routeWebSocket(/\/api\/cameras\/[^/]+\/live\/stream/, socket => {
    nvr.sockets += 1;
    if (nvr.video) return; // Open, and silent
    const camera = /\/api\/cameras\/([^/]+)\//.exec(socket.url())[1];
    socket.send(JSON.stringify({ type: "unsupported", codec: "hevc", fallback: `/api/cameras/${camera}/live?quality=low` }));
    setTimeout(() => socket.close({ code: 4415, reason: "unsupported" }), 50);
  });
  await page.route(/\/api\/cameras\/[^/]+\/live\/status/, route => {
    nvr.statusAsked += 1;
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ state: "live", viewers: 2 }) });
  });
  await page.addInitScript(part => {
    const control = window.pictureStream = { mode: "pictures", end: false, asked: 0 };
    const real = window.fetch.bind(window);
    const aborted = () => new DOMException("Aborted", "AbortError");
    window.fetch = (input, init) => {
      const url = String(input instanceof Request ? input.url : input);
      if (!/\/api\/cameras\/[^/]+\/live\?/.test(url)) return real(input, init);
      control.asked += 1;
      const signal = init && init.signal;
      if (control.mode === "silent") return new Promise((_, reject) => signal && signal.addEventListener("abort", () => reject(aborted())));
      const body = new ReadableStream({
        start(stream) {
          if (control.mode !== "pictures") return stream.close();
          const timer = setInterval(() => {
            if (control.end || control.mode !== "pictures") {
              control.end = false;
              clearInterval(timer);
              stream.close();
              return;
            }
            stream.enqueue(new Uint8Array(part.slice(0, 40)));
            stream.enqueue(new Uint8Array(part.slice(40)));
          }, 200);
          if (signal) signal.addEventListener("abort", () => { clearInterval(timer); try { stream.error(aborted()); } catch { /* already closed */ } });
          return undefined;
        },
      });
      return Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "multipart/x-mixed-replace; boundary=frame" } }));
    };
  }, PART);
}

const fresh = () => ({ video: false, sockets: 0, statusAsked: 0 });
const set = (page, change) => page.evaluate(value => Object.assign(window.pictureStream, value), change);
const asked = page => page.evaluate(() => window.pictureStream.asked);

/** Let the page's clocks run this long, a second at a time so every timer along the way fires in its turn. */
async function pass(page, seconds) {
  for (let second = 0; second < seconds; second += 1) await page.clock.runFor(1_000);
}

test("a compatibility-mode tile whose picture stream ends asks again by itself, and is not kept Live by someone else's viewing", async ({ page }) => {
  seedFixtures(1);
  await page.clock.install();
  const nvr = fresh();
  await play(page, nvr);
  await signInNext(page);
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const tile = page.locator(".tile[data-camera-id]").first();
  const label = tile.locator(".tile-state");

  // Pictures arrive: Live, in compatibility mode, and the picture is on screen.
  await pass(page, 2);
  await expect(label).toContainText("Live · compatibility");
  await expect.poll(() => tile.locator("img.tile-video").evaluate(image => image.naturalWidth)).toBe(16);
  expect(await asked(page)).toBe(1);

  // The server ends the stream cleanly. Nobody clicks anything: the tile asks again and is Live again.
  await set(page, { end: true });
  await pass(page, 4);
  expect(await asked(page)).toBe(2);
  await expect(label).toContainText("Live · compatibility");

  // Now the stream gives no pictures, while the camera-wide status keeps saying "live" (someone else is watching). The tile judges
  // by its own pictures: it stops saying Live, says it is retrying, and really is.
  await set(page, { mode: "nothing" });
  await pass(page, 8);
  await expect(label).not.toContainText("Live");
  await pass(page, 20);
  await expect(label).toContainText("Offline");
  await expect(tile.getByRole("status")).toContainText("Retrying automatically");
  const whileOffline = await asked(page);
  await pass(page, 65);
  expect(await asked(page)).toBeGreaterThan(whileOffline);
  expect(nvr.statusAsked).toBe(0);

  // A stream that never answers at all is given up on and asked for again.
  await set(page, { mode: "silent" });
  await pass(page, 35);
  const whileSilent = await asked(page);
  await pass(page, 60);
  expect(await asked(page)).toBeGreaterThan(whileSilent);
  await expect(label).not.toContainText("Live");

  // The camera comes back: Live again, with nobody having clicked Retry.
  await set(page, { mode: "pictures" });
  await pass(page, 60);
  await expect(label).toContainText("Live · compatibility");
});

test("a tile whose connection opens and then gives nothing asks again, and never says Live", async ({ page }) => {
  seedFixtures(1);
  await page.clock.install();
  const nvr = { ...fresh(), video: true };
  await play(page, nvr);
  await signInNext(page);
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const label = page.locator(".tile[data-camera-id]").first().locator(".tile-state");
  await expect.poll(() => nvr.sockets).toBe(1);
  await pass(page, 25);
  await expect.poll(() => nvr.sockets).toBeGreaterThan(1);
  await pass(page, 30);
  await expect.poll(() => nvr.sockets).toBeGreaterThan(2);
  await expect(label).not.toContainText("Live");
});

test("a video wall's tile in compatibility mode says Live or not truthfully, and recovers", async ({ page }) => {
  seedFixtures(1);
  await page.clock.install();
  await play(page, fresh());
  const json = body => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const camera = { id: "wall-camera", name: "Camera 001", archived_at: null, live_substream_available: false };
  await page.route("**/api/wall/session", route => route.fulfill(json({ name: "Lobby wall", scopes: ["read", "media"], interface: "build-one" })));
  await page.route("**/api/live/views", route => route.fulfill(json({ layouts: [], shared_views: [{ name: "Lobby", scope: "shared", camera_ids: [camera.id], grid_size: 1, tile_camera_ids: [camera.id], layout_mode: "auto" }] })));
  await page.route("**/api/cameras", route => route.fulfill(json({ cameras: [camera] })));
  await page.route("**/api/settings/display", route => route.fulfill(json({ timezone: "UTC", hour_cycle: "24" })));
  await page.route("**/api/live/alarm-pop-ins**", route => route.fulfill(json({ alarms: [], cursor: "0" })));
  await page.goto("/wall?view=Lobby&dwell=30");

  const label = page.locator(".wall-tile .tile-state");
  await pass(page, 2);
  await expect(label).toHaveText("Live");
  await expect.poll(() => page.locator(".wall-tile img.tile-video").evaluate(image => image.naturalWidth)).toBe(16);
  await set(page, { mode: "nothing" });
  await pass(page, 30);
  await expect(label).toHaveText("Offline");
  await set(page, { mode: "pictures" });
  await pass(page, 65);
  await expect(label).toHaveText("Live");
});
