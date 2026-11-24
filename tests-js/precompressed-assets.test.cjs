// The interface's fingerprinted files are compressed once at build time (#537), and the server sends the copy a browser takes.
// Runs after the web build, as CI's build job does (the files are in app/web_dist).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const assets = path.join(__dirname, "..", "app", "web_dist", "assets");

test("every script and style sheet of 1 kB or more has a Brotli and a gzip copy that undo to the same bytes", () => {
  assert.ok(fs.existsSync(assets), "build the interface first (npm run build --prefix web)");
  const files = fs.readdirSync(assets).filter(name => /\.(js|css)$/.test(name) && fs.statSync(path.join(assets, name)).size >= 1024);
  assert.ok(files.length > 3);
  for (const name of files) {
    const original = fs.readFileSync(path.join(assets, name));
    assert.ok(original.equals(zlib.brotliDecompressSync(fs.readFileSync(path.join(assets, `${name}.br`)))), `${name}.br`);
    assert.ok(original.equals(zlib.gunzipSync(fs.readFileSync(path.join(assets, `${name}.gz`)))), `${name}.gz`);
  }
  const main = files.find(name => /^index-.*\.js$/.test(name));
  const sizes = Object.fromEntries(["", ".br", ".gz"].map(suffix => [suffix || "as built", fs.statSync(path.join(assets, main + suffix)).size]));
  console.log(`main script ${main}: ${JSON.stringify(sizes)}`);
  assert.ok(sizes[".br"] < 200_000, `${sizes[".br"]} bytes with Brotli`);
  // The target on #537: under 200 kB with gzip too, for a browser or proxy that does not take Brotli. Pages opened on demand load later
  // (app/lazyPage.ts); a new page added to the first screen's imports is what would push this back over.
  assert.ok(sizes[".gz"] < 200_000, `${sizes[".gz"]} bytes with gzip: move a page that is not on the first screen to lazyPage`);
  assert.ok(sizes[".gz"] < sizes["as built"] / 3, `${sizes[".gz"]} bytes with gzip`);
});
