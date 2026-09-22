// Headless UI smoke: render every app.js route against the committed exports
// with a stub DOM. Fails loudly if a route throws or renders empty/markup-less
// output. Run: node scripts/ui_smoke.mjs
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = readFileSync(join(ROOT, "app.js"), "utf8");

function mockEl() {
  return {
    innerHTML: "", textContent: "", disabled: false, value: "",
    dataset: {}, classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {},
  };
}

const elements = new Map();
function elFor(sel) {
  if (!elements.has(sel)) elements.set(sel, mockEl());
  return elements.get(sel);
}

const ctx = {
  console,
  location: { hash: "#/dashboard" },
  document: {
    querySelector: (s) => elFor(s),
    querySelectorAll: () => [],
  },
  window: null,
  fetch: async (url) => {
    const name = String(url).replace(/^data\/site\//, "").replace(/\.json$/, "");
    const p = join(ROOT, "data", "site", name + ".json");
    if (!existsSync(p)) return { ok: false, json: async () => ({}) };
    return { ok: true, json: async () => JSON.parse(readFileSync(p, "utf8")) };
  },
};
ctx.window = ctx;
ctx.globalThis = ctx;
ctx.setTimeout = setTimeout;
ctx.scrollTo = () => {};
ctx.addEventListener = () => {};
ctx.removeEventListener = () => {};
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__t = { render, ROUTES, pageDashboard, pageLeaderboard, pageUpcoming, pageCompleted, pageStrategies, pageStrategy, pageSport, pagePerformance, pageResearch, pageSources, pageQuality };", ctx);

const routes = [
  ["#/dashboard", "Dashboard", ["Upcoming", "completed", "quality"]],
  ["#/leaderboard", "Leaderboard", ["strategy", "ROI", "Last activity"]],
  ["#/upcoming", "Upcoming", ["legs", "Stake"]],
  ["#/completed", "Completed", ["Payout", "settle"]],
  ["#/strategies", "Strategies", ["Hypothesis", "S-NFL-01"]],
  ["#/strategy/S-NFL-01", "Elo", ["Hypothesis", "history", "Backtest"]],
  ["#/strategy/S-MULTI-01", "Cross-sport", ["Hypothesis"]],
  ["#/sport/NFL", "NFL", ["Upcoming", "Recent finals"]],
  ["#/sport/MLB", "MLB", ["Upcoming", "Recent finals"]],
  ["#/sport/NHL", "NHL", ["Upcoming", "Recent finals"]],
  ["#/sport/NBA", "NBA", ["Upcoming", "Recent finals"]],
  ["#/performance", "Performance", ["By sport", "By market", "By strategy", "Equity"]],
  ["#/research", "Research", ["Hypothesis", "R-0"]],
  ["#/sources", "Data sources", ["SRC_", "VERIFIED"]],
  ["#/quality", "Data quality", ["verifications", "scan"]],
];

let failures = 0;
for (const [hash, marker, mustInclude] of routes) {
  ctx.location.hash = hash;
  elements.clear();
  try {
    await ctx.__t.render();
    // Some pages render ticket cards into deferred containers (#up-list,
    // #done-list, #h-list) after the shell; check the whole rendered surface.
    const html = ["#app", "#up-list", "#done-list", "#h-list"]
      .map((s) => (elements.get(s) || {}).innerHTML || "").join("\n");
    const problems = [];
    if (!html || html.length < 200) problems.push("empty render");
    if (!html.includes(marker)) problems.push("missing marker: " + marker);
    for (const m of mustInclude) if (!html.toLowerCase().includes(m.toLowerCase())) problems.push("missing: " + m);
    if (problems.length) {
      failures++;
      console.log("FAIL", hash, "--", problems.join("; "));
    } else {
      console.log("PASS", hash, `(${html.length} bytes)`);
    }
  } catch (e) {
    failures++;
    console.log("FAIL", hash, "-- threw:", e.message);
  }
}
console.log(failures ? `\nUI SMOKE: ${failures}/${routes.length} routes failed` : `\nUI SMOKE: all ${routes.length} routes rendered`);
process.exit(failures ? 1 : 0);
