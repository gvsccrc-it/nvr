// When a session expires, nothing the interface had fetched for it is shown to the next person signing in on the same tab (#538).
const { test, expect, seedFixtures, FIXTURE_PASSWORD } = require("./helpers.cjs");

test("after a session expires, the next person signing in never sees the previous person's cameras", async ({ page }) => {
  seedFixtures(4); // Odd-numbered fixture cameras belong to "Operations"
  await page.goto("/");
  await page.getByLabel("Username").fill("camera-admin");
  await page.getByLabel("Password").fill(FIXTURE_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  const cameras = page.getByRole("complementary", { name: "Camera browser" });
  await expect(cameras).toContainText("Camera 002"); // The administrator's list is cached

  // The operator may see only the Operations group.
  const groups = await (await page.request.get("/api/camera-groups")).json();
  const operations = (groups.groups ?? groups).find(group => group.name === "Operations");
  const users = await (await page.request.get("/api/admin/users")).json();
  const operator = (users.users ?? users).find(user => user.username === "camera-operator");
  const narrowed = await page.request.put("/api/admin/access-grants",
    { data: { subject_type: "user", subject: String(operator.id), grants: [{ scope_type: "group", group_id: operations.id }] } });
  expect(narrowed.ok()).toBeTruthy();

  // The session ends on the server; the page finds out from its next request (a 401), not from signing out.
  expect((await page.request.post("/api/auth/logout")).ok()).toBeTruthy();
  await expect(page.getByLabel("Username")).toBeVisible({ timeout: 30_000 });

  // From here, the administrator's cameras must never be drawn, not even for a moment while the operator's own list loads.
  await page.evaluate(() => {
    window.__shown = [];
    new MutationObserver(() => {
      for (const name of ["Camera 002", "Camera 004"]) if (document.body.innerText.includes(name)) window.__shown.push(name);
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  await page.getByLabel("Username").fill("camera-operator");
  await page.getByLabel("Password").fill(FIXTURE_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(cameras).toContainText("Camera 001");
  await expect(cameras).toContainText("Camera 003");
  await expect(cameras).not.toContainText("Camera 002");
  expect(await page.evaluate(() => window.__shown)).toEqual([]);
});
