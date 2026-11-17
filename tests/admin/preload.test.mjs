// index.html / login.html ask for the modules of the first screen at once (<link rel="modulepreload">) instead of
// letting the browser find them one import level after another. There is no bundler to keep those lists: this test
// reads the imports and fails — naming the list to fix — when a module was added, removed or renamed.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join, posix } from "node:path";
import { fileURLToPath } from "node:url";

const ADMIN = join(dirname(fileURLToPath(import.meta.url)), "../../web/admin");
const read = (rel) => readFileSync(join(ADMIN, rel), "utf8");

/** Static imports of a module (dynamic import() is a page loaded later, not part of the first screen). */
function imports(rel) {
  const out = [];
  const re = /^\s*(?:import|export)\s[^;]*?\sfrom\s+"([^"]+)"|^\s*import\s+"([^"]+)"/gm;
  let m;
  while ((m = re.exec(read(rel)))) out.push(posix.normalize(posix.join(posix.dirname(rel), m[1] || m[2])));
  return out;
}

/** Everything the entry modules import, directly or not (the entries included). */
function graph(entries) {
  const seen = new Set();
  const queue = [...entries];
  while (queue.length) {
    const f = queue.shift();
    if (seen.has(f)) continue;
    seen.add(f);
    queue.push(...imports(f));
  }
  return seen;
}

const sorted = (it) => [...it].sort();
const entriesOf = (html) => [...html.matchAll(/<script type="module" src="([^"]+)"/g)].map((m) => m[1]);
const preloadsOf = (html) => [...html.matchAll(/<link rel="modulepreload" href="([^"]+)"/g)].map((m) => m[1]);

for (const page of ["index.html", "login.html"]) {
  test(`${page}: modulepreload lists exactly what its entry modules import`, () => {
    const html = read(page);
    const entries = entriesOf(html);
    assert.ok(entries.length > 0);
    const want = sorted([...graph(entries)].filter((f) => !entries.includes(f)));
    assert.deepEqual(sorted(preloadsOf(html)), want,
      `${page}: the <link rel="modulepreload"> list must be:\n` + want.map((f) => `  <link rel="modulepreload" href="${f}">`).join("\n"));
  });
}

test("index.html: the inline page preload knows every page and what the Dashboard imports", () => {
  const html = read("index.html");
  const list = (name) => {
    const m = html.match(new RegExp(`var ${name} = "([^"]*)"\\.split\\(" "\\)`));
    assert.ok(m, `index.html: var ${name} = "…".split(" ")`);
    return m[1].split(" ");
  };
  const pages = readdirSync(join(ADMIN, "js/pages")).filter((f) => f.endsWith(".js")).map((f) => f.slice(0, -3));
  assert.deepEqual(sorted(list("PAGES")), sorted(pages), "index.html PAGES: the files of js/pages");

  const shell = graph(entriesOf(html));
  const want = sorted([...graph(["js/pages/dashboard.js"])].filter((f) => !shell.has(f) && f !== "js/pages/dashboard.js"));
  assert.deepEqual(sorted(list("DASHBOARD")), want, `index.html DASHBOARD must be: "${want.join(" ")}"`);
});

test("preloaded files exist (fonts included)", () => {
  for (const page of ["index.html", "login.html"]) {
    const html = read(page);
    const hrefs = [...preloadsOf(html), ...[...html.matchAll(/<link rel="preload"[^>]* href="([^"]+)"/g)].map((m) => m[1])];
    for (const href of hrefs) assert.ok(existsSync(join(ADMIN, href)), `${page}: ${href}`);
  }
});

test("the stylesheet's fonts are the ones index.html preloads", () => {
  const css = [...read("css/app.css").matchAll(/@font-face\s*\{[^}]*url\("([^"]+)"\)/g)].map((m) => posix.normalize(posix.join("css", m[1])));
  const pre = [...read("index.html").matchAll(/<link rel="preload" as="font"[^>]* href="([^"]+)"[^>]* crossorigin>/g)].map((m) => posix.normalize(m[1]));
  assert.deepEqual(sorted(pre), sorted(new Set(css)));
});
