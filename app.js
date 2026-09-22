/* ParlaySports static frontend. Dependency-free; reads data/site/*.json. */
"use strict";
const D = {};
async function get(name) {
  if (!D[name]) {
    const r = await fetch("data/site/" + name + ".json", { cache: "no-store" });
    if (!r.ok) throw new Error("failed to load " + name);
    D[name] = await r.json();
  }
  return D[name];
}
const $ = (s) => document.querySelector(s);
const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };
const esc = (x) => String(x == null ? "" : x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (x) => (x == null ? "—" : (x < 0 ? "−$" : "$") + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const money0 = (x) => (x == null ? "—" : (x < 0 ? "−$" : "$") + Math.abs(Math.round(x)).toLocaleString("en-US"));
const pct = (x, d) => (x == null ? "—" : (x * 100).toFixed(d == null ? 1 : d) + "%");
const cls = (x) => (x == null ? "mut" : x > 0 ? "pos" : x < 0 ? "neg" : "mut");
const dt = (s) => (s || "—").replace("T", " ").replace("Z", " UTC");
const gradeBadge = (g) => '<span class="badge g-' + esc(g || "UNPRICED") + '">' + esc(g || "UNPRICED") + "</span>";
const statusBadge = (s) => '<span class="badge s-' + esc(s || "upcoming") + '">' + esc((s || "upcoming").toUpperCase()) + "</span>";
const stratLink = (id) => '<a href="#/strategy/' + esc(id) + '"><code>' + esc(id) + "</code></a>";

let USERS = {};
async function users() {
  if (!Object.keys(USERS).length) {
    const s = await get("strategies");
    s.forEach((r) => (USERS[r.strategy_id] = r.username));
  }
  return USERS;
}
const uname = (id) => USERS[id] || id;

function spark(points, w, h, color) {
  if (!points || points.length < 2) return '<span class="mut">—</span>';
  w = w || 220; h = h || 64;
  const vs = points.map((p) => p.b);
  const lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
  const rg = hi - lo || 1;
  const step = w / (vs.length - 1);
  let d = "";
  vs.forEach((v, i) => {
    const x = (i * step).toFixed(1);
    const y = (h - 4 - ((v - lo) / rg) * (h - 8)).toFixed(1);
    d += (i ? "L" : "M") + x + " " + y;
  });
  const c = color || (vs[vs.length - 1] >= vs[0] ? "#3fb950" : "#f85149");
  return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none"><path d="' + d + '" fill="none" stroke="' + c + '" stroke-width="1.6"/></svg>';
}

function legDetailText(l) {
  let d = {};
  try { d = JSON.parse(l.leg_detail || "{}"); } catch (e) { return esc(l.leg_detail || ""); }
  const bits = [];
  if (d.note) bits.push(d.note);
  if (d.edge != null) bits.push("edge " + pct(d.edge));
  if (d.market_prob != null) bits.push("mkt " + pct(d.market_prob));
  if (d.features && d.features.price_source) bits.push(String(d.features.price_source));
  return esc(bits.join(" · "));
}
function legRow(l, showGame) {
  const matchup = l.g_away + " @ " + l.g_home;
  const score = l.game_status === "final" ? " <span class='mut'>(" + l.away_score + "–" + l.home_score + ")</span>" : "";
  const when = showGame ? esc(l.game_date) + (l.start_utc ? " " + esc(l.start_utc.slice(11, 16)) + "Z" : "") + "<br>" : "";
  const pick = l.market + " " + l.selection + (l.line != null ? " " + (l.line > 0 ? "+" : "") + l.line : "");
  const odds = l.odds_american != null ? (l.odds_american > 0 ? "+" : "") + Math.round(l.odds_american) : "—";
  const res = l.result && l.result !== "pending" ? statusBadge(l.result) + "<br><span class='mut'>" + esc(l.settle_detail || "") + "</span>" : "<span class='mut'>pending</span>";
  const vfy = l.game_verified != null ? (l.game_verified ? "verified game" : "unverified game") : "";
  return "<tr><td>" + when + "<b>" + esc(matchup) + "</b>" + score +
    "<br><span class='mut'>" + esc(l.g_sport) + " · " + esc(l.game_status || "") + (vfy ? " · " + vfy : "") + "</span></td><td>" + esc(pick) +
    "<br><span class='mut'>" + legDetailText(l) + "</span></td><td class='num'>" + esc(String(odds)) +
    "<br><span class='mut'>" + esc(l.odds_type || "missing") + "</span></td><td class='num'>" +
    (l.model_prob != null ? pct(l.model_prob) : "—") + "</td><td>" + res + "</td></tr>";
}

function parlayCard(p, opts) {
  opts = opts || {};
  const legs = (p.legs || []).map((l) => legRow(l, true)).join("");
  const comb = p.combined_american != null ? (p.combined_american > 0 ? "+" : "") + Math.round(p.combined_american) : "—";
  const settled = p.status === "won" || p.status === "lost" || p.status === "push" || p.status === "void";
  return '<div class="card parlay"><div class="parlay-head"><span class="pid">' + esc(p.parlay_id) + "</span>" +
    statusBadge(p.status) + gradeBadge(p.pricing_grade) +
    "<span>" + stratLink(p.strategy_id) + " <span class='mut'>" + esc(uname(p.strategy_id)) + "</span></span>" +
    "<span class='mut'>" + p.n_legs + " legs · sports " + esc((p.sports || []).join("+")) + " · slate " + esc(p.slate_date) + " · created " + esc(dt(p.decision_utc)) + "</span></div>" +
    "<div class='kv' style='max-width:720px'><span>Stake <b>" + money(p.stake) + "</b></span>" +
    "<span>Combined <b>" + esc(String(comb)) + "</b>" + (p.combined_decimal != null ? " (" + p.combined_decimal + ")" : "") + "</span>" +
    (settled
      ? "<span>Payout <b>" + money(p.payout) + "</b></span><span>P&amp;L <b class='" + cls(p.pnl) + "'>" + money(p.pnl) + "</b></span>" +
        "<span>ROI <b class='" + cls(p.roi_parlay) + "'>" + pct(p.roi_parlay) + "</b></span>"
      : "<span>Potential <b>" + money(p.potential_payout) + "</b></span><span class='mut'>P&amp;L pending</span>") +
    "</div>" +
    (p.result_detail ? "<div class='sub'>" + esc(p.result_detail) + (p.settlement_source ? " · settlement: <span class='mut'>" + esc(p.settlement_source) + "</span>" : "") +
      (p.settled_utc ? " · settled " + esc(dt(p.settled_utc)) : "") + "</div>" : "") +
    "<div style='overflow-x:auto'><table class='data'><thead><tr><th>Game</th><th>Pick</th><th class='num'>Odds</th><th class='num'>Model</th><th>Result</th></tr></thead><tbody>" +
    legs + "</tbody></table></div></div>";
}

/* ---------------- Dashboard ---------------- */
async function pageDashboard() {
  const [meta, lbf, lbb, up, q, perf, games, done] = await Promise.all([
    get("meta"), get("leaderboard_forward"), get("leaderboard_backtest"),
    get("upcoming"), get("quality"), get("performance"), get("games"), get("completed")]);
  await users();
  const c = meta.counts;
  const fActive = lbf.filter((r) => r.parlays > 0).sort((a, b) => b.pnl - a.pnl);
  const bSorted = lbb.filter((r) => r.started).sort((a, b) => b.pnl - a.pnl);
  const slates = {};
  up.forEach((p) => { slates[p.slate_date] = (slates[p.slate_date] || 0) + 1; });
  const slateRows = Object.keys(slates).sort().map((s) => "<div class='kv'><span>" + esc(s) + "</span><b>" + slates[s] + " tickets</b></div>").join("");
  const lbRow = (r) => "<tr><td>" + stratLink(r.strategy_id) + "<br><span class='mut'>" + esc(r.name) + "</span></td><td>" + esc(r.sport) + "</td><td class='num'>" + r.parlays + "</td><td class='num " + cls(r.pnl) + "'>" + money(r.pnl) + "</td><td class='num'>" + pct(r.roi) + "</td><td class='num'>" + money0(r.bankroll) + "</td></tr>";
  const open = (q.issues || []).filter((i) => i.status === "open");
  const recent = done.slice(0, 5).map((p) => "<tr><td>" + statusBadge(p.status) + "</td><td>" + stratLink(p.strategy_id) + "<br><span class='mut'>" + esc(uname(p.strategy_id)) + "</span></td><td>" + esc(p.slate_date) + " · " + esc((p.sports || []).join("+")) + " · " + p.n_legs + " legs</td><td class='num'>" + money(p.payout) + "</td><td class='num " + cls(p.pnl) + "'>" + money(p.pnl) + "</td></tr>").join("");
  const sp = perf.by_sport || {};
  const sportRows = ["NFL", "MLB", "NHL", "NBA"].map((s) => {
    const d = sp[s] || {};
    return "<tr><td><a href='#/sport/" + s + "'>" + s + "</a></td><td class='num'>" + (d.parlays || 0) + "</td><td class='num'>" + (d.won || 0) + "W/" + (d.lost || 0) + "L</td><td class='num'>" + pct(d.leg_hit) + "</td><td class='num " + cls(d.pnl) + "'>" + money(d.pnl) + "</td><td class='num'>" + pct(d.roi) + "</td></tr>";
  }).join("");
  const verified = [];
  ["NFL", "MLB", "NHL", "NBA"].forEach((s) => {
    ((games.by_sport || {})[s] || {}).recent_finals = ((games.by_sport || {})[s] || {}).recent_finals || [];
    ((games.by_sport || {})[s] || {}).recent_finals.filter((t) => t.verified).slice(0, 2).forEach((t) => verified.push([s, t]));
  });
  const vRows = verified.map(([s, t]) => "<tr><td>" + s + "</td><td><b>" + esc(t.away_team) + " @ " + esc(t.home_team) + "</b> <span class='mut'>(" + esc(t.game_date) + ")</span></td><td class='num'>" + t.away_score + "–" + t.home_score + "</td><td><span class='mut'>" + esc(t.source_id) + "</span></td></tr>").join("");
  return "<h1>Dashboard</h1><p class='sub'>Competition status: paper play across <b>4 sports</b> and <b>28 versioned strategies</b> — <b>" + up.length + " upcoming</b> tickets, <b>" + done.length + " completed</b>. Two sealed books: <b>backtest</b> (history, settled) and <b>forward</b> (live paper). " + esc(meta.disclaimer) + "</p>" +
    "<div class='grid c4'>" +
    "<div class='card'><div class='stat'>" + c.games.toLocaleString() + "</div><div class='mut'>games tracked</div></div>" +
    "<div class='card'><div class='stat'>" + c.prices.toLocaleString() + "</div><div class='mut'>price points</div></div>" +
    "<div class='card'><div class='stat'>" + c.parlays.toLocaleString() + "</div><div class='mut'>parlays (" + c.legs.toLocaleString() + " legs)</div></div>" +
    "<div class='card'><div class='stat'>" + up.length + "</div><div class='mut'>upcoming tickets</div></div>" +
    "</div>" +
    "<div class='grid c2'><div class='card'><h3>Forward book — top 5 by P&amp;L</h3>" +
    (fActive.length ? "<table class='data'><thead><tr><th>Strategy</th><th>Sport</th><th class='num'>N</th><th class='num'>P&amp;L</th><th class='num'>ROI</th><th class='num'>Bank</th></tr></thead><tbody>" + fActive.slice(0, 5).map(lbRow).join("") + "</tbody></table>" : "<p class='mut'>No settled forward parlays yet — tickets are upcoming.</p>") +
    "<p><a href='#/leaderboard'>Full leaderboard →</a></p></div>" +
    "<div class='card'><h3>Backtest book — top 5 by P&amp;L</h3><table class='data'><thead><tr><th>Strategy</th><th>Sport</th><th class='num'>N</th><th class='num'>P&amp;L</th><th class='num'>ROI</th><th class='num'>Bank</th></tr></thead><tbody>" +
    bSorted.slice(0, 5).map(lbRow).join("") + "</tbody></table>" +
    "<h3>Bottom 3 (honest losers shown too)</h3><table class='data'><tbody>" + bSorted.slice(-3).reverse().map(lbRow).join("") + "</tbody></table></div></div>" +
    "<div class='grid c2'><div class='card'><h3>Recent completed parlays</h3>" +
    (recent ? "<table class='data'><thead><tr><th>Result</th><th>Strategy</th><th>Ticket</th><th class='num'>Payout</th><th class='num'>P&amp;L</th></tr></thead><tbody>" + recent + "</tbody></table>" : "<p class='mut'>None settled yet.</p>") +
    "<p><a href='#/completed'>All completed →</a></p></div>" +
    "<div class='card'><h3>Sport performance (settled legs &amp; tickets)</h3><table class='data'><thead><tr><th>Sport</th><th class='num'>Tickets</th><th class='num'>W/L</th><th class='num'>Leg hit%</th><th class='num'>P&amp;L</th><th class='num'>ROI</th></tr></thead><tbody>" + sportRows + "</tbody></table>" +
    "<p><a href='#/performance'>Full performance →</a></p></div></div>" +
    "<div class='grid c2'><div class='card'><h3>Upcoming slates</h3>" + (slateRows || "<p class='mut'>No upcoming tickets.</p>") + "<p><a href='#/upcoming'>All upcoming tickets →</a></p></div>" +
    "<div class='card'><h3>Data quality</h3><div class='kv'><span>Open issues</span><b class='" + (open.length ? "neg" : "pos") + "'>" + open.length + "</b></div>" +
    "<div class='kv'><span>Resolved with notes</span><b>" + (q.issues || []).filter((i) => i.status !== "open").length + "</b></div>" +
    "<div class='kv'><span>Independent verifications</span><b>" + (q.verifications || []).length + "</b></div>" +
    (open.length ? "<div class='callout'>Latest: " + esc(open[0].detail) + "</div>" : "") +
    "<p><a href='#/quality'>Quality board →</a></p></div></div>" +
    "<div class='card'><h3>Important verified games / events</h3><p class='mut'>Recently settled games with source-verified provenance.</p>" +
    (vRows ? "<table class='data'><thead><tr><th>Sport</th><th>Game</th><th class='num'>Score</th><th>Source</th></tr></thead><tbody>" + vRows + "</tbody></table>" : "<p class='mut'>No verified finals in the current window.</p>") +
    "</div>";
}

/* ---------------- Leaderboard ---------------- */
function ticketStats(list) {
  const by = {};
  list.forEach((p) => {
    const r = by[p.strategy_id] = by[p.strategy_id] || {
      strategy_id: p.strategy_id, tickets: 0, won: 0, lost: 0, push: 0, pending: 0,
      staked: 0, pnl: 0, legW: 0, legD: 0, last_activity: null, grades: {},
    };
    r.tickets++;
    r.grades[p.pricing_grade] = (r.grades[p.pricing_grade] || 0) + 1;
    if (p.status === "won" || p.status === "lost" || p.status === "push") {
      r[p.status === "won" ? "won" : p.status === "lost" ? "lost" : "push"]++;
      r.pnl += p.pnl || 0;
      r.staked += p.stake || 0;
      if (!r.last_activity || p.slate_date > r.last_activity) r.last_activity = p.slate_date;
    } else r.pending++;
    (p.legs || []).forEach((l) => {
      if (l.result === "win" || l.result === "loss") { r.legW += l.result === "win" ? 1 : 0; r.legD++; }
    });
  });
  return Object.values(by).map((r) => ({
    strategy_id: r.strategy_id, username: uname(r.strategy_id), sport: null,
    parlays: r.tickets, won: r.won, lost: r.lost, push: r.push, pending: r.pending,
    staked: r.staked, pnl: Math.round(r.pnl * 100) / 100,
    roi: r.staked ? r.pnl / r.staked : null,
    parlay_hit_rate: (r.won + r.lost) ? r.won / (r.won + r.lost) : null,
    leg_hit_rate: r.legD ? r.legW / r.legD : null,
    last_activity: r.last_activity, grades: r.grades, filtered: true,
  }));
}
async function pageLeaderboard() {
  const [lbf, lbb, done, up, strats] = await Promise.all([
    get("leaderboard_forward"), get("leaderboard_backtest"), get("completed"),
    get("upcoming"), get("strategies")]);
  await users();
  const st = window.__lb || (window.__lb = { book: "forward", sport: "ALL", from: "", to: "", strat: "", market: "", size: "" });
  const stratNames = {}; strats.forEach((s) => stratNames[s.strategy_id] = s);
  const filtering = !!(st.from || st.to || st.strat || st.market || st.size);
  let rows, note = "";
  if (filtering) {
    const tickets = done.concat(up).filter((p) => p.test_mode === st.book);
    const inRange = (p) => (!st.from || p.slate_date >= st.from) && (!st.to || p.slate_date <= st.to);
    const list = tickets.filter((p) => inRange(p) &&
      (!st.strat || p.strategy_id === st.strat) &&
      (!st.market || (p.market_mix || "").indexOf(st.market) >= 0) &&
      (!st.size || (st.size === "5+" ? p.n_legs >= 5 : p.n_legs === Number(st.size))));
    rows = ticketStats(list).map((r) => ({ ...r, ...(stratNames[r.strategy_id] || {}), name: (stratNames[r.strategy_id] || {}).name, sport: (stratNames[r.strategy_id] || {}).sport, username: uname(r.strategy_id) }));
    note = "<div class='callout blue'>Filters active: figures are recomputed over the matching tickets only (last 2,000 completed + all upcoming). Bankroll / MaxDD are whole-book quantities and stay on the unfiltered board.</div>";
  } else {
    rows = (st.book === "forward" ? lbf : lbb);
    note = "<div class='callout blue'>Grades: VERIFIED = direct market quote · REFERENCE = aggregated line · MODEL = estimated price (payout approximate) · MIXED = blend · UNPRICED = $0 tracking only.</div>";
  }
  rows = rows.filter((r) => st.sport === "ALL" || r.sport === st.sport)
    .sort((a, b) => (b.bankroll != null ? b.bankroll - (b.start || 0) : b.pnl || 0) - (a.bankroll != null ? a.bankroll - (a.start || 0) : a.pnl || 0));
  const body = rows.map((r) => {
    const grades = Object.keys(r.grades || {}).map((g) => gradeBadge(g) + "×" + r.grades[g]).join(" ");
    return "<tr><td>" + stratLink(r.strategy_id) + "<br><b>" + esc(r.username || uname(r.strategy_id)) + "</b><br><span class='mut'>" + esc(r.name || "") + "</span></td>" +
      "<td>" + esc(r.sport || "—") + "<br><span class='mut'>" + esc(r.category || "") + "</span></td>" +
      "<td class='num'>" + r.parlays + "<br><span class='mut'>" + (r.won || 0) + "W/" + (r.lost || 0) + "L/" + (r.push || 0) + "P</span></td>" +
      "<td class='num'>" + pct(r.parlay_hit_rate) + "<br><span class='mut'>legs " + pct(r.leg_hit_rate) + "</span></td>" +
      "<td class='num'>" + money(r.staked) + "</td><td class='num " + cls(r.pnl) + "'>" + money(r.pnl) + "</td>" +
      "<td class='num'>" + pct(r.roi) + "</td><td class='num'>" + (r.bankroll != null ? money0(r.bankroll) : "—") + "</td>" +
      "<td class='num'>" + (r.max_drawdown != null ? pct(r.max_drawdown) : "—") + "</td>" +
      "<td>" + esc(r.last_activity || "—") + "</td><td>" + grades + "</td></tr>";
  }).join("");
  const sports = ["ALL", "NFL", "MLB", "NHL", "NBA", "MULTI"];
  const opt = (v, label, cur) => "<option value='" + esc(v) + "'" + (cur === v ? " selected" : "") + ">" + esc(label) + "</option>";
  const stratOpts = ["", ...Object.keys(stratNames).sort()].map((s) => opt(s, s ? s + " (" + (stratNames[s].username) + ")" : "All strategies", st.strat)).join("");
  return "<h1>Leaderboard</h1><p class='sub'>One row per strategy per book. Standard start: <b>$10,000</b>. Books never mix: switch tabs to compare paper-live vs history. Filters recompute the factual stats over matching tickets; the default board is the full book of record.</p>" +
    "<div class='tabs'><button class='" + (st.book === "forward" ? "on" : "") + "' data-book='forward'>Forward (paper-live)</button>" +
    "<button class='" + (st.book === "backtest" ? "on" : "") + "' data-book='backtest'>Backtest (history)</button></div>" +
    "<div class='tabs'>" + sports.map((s) => "<button class='" + (st.sport === s ? "on" : "") + "' data-sport='" + s + "'>" + s + "</button>").join("") + "</div>" +
    "<div class='filters'>From <input type='date' id='lb-from' value='" + esc(st.from) + "'> To <input type='date' id='lb-to' value='" + esc(st.to) + "'>" +
    "<select id='lb-strat'>" + stratOpts + "</select>" +
    "<select id='lb-market'>" + ["", "ML", "SPREAD", "TOTAL"].map((m) => opt(m, m ? "Contains " + m : "Any market", st.market)).join("") + "</select>" +
    "<select id='lb-size'>" + ["", "2", "3", "4", "5+"].map((s) => opt(s, s ? s + " legs" : "Any parlay size", st.size)).join("") + "</select>" +
    "<button id='lb-clear'>Clear filters</button></div>" +
    note +
    "<div style='overflow-x:auto'><table class='data'><thead><tr><th>Strategy</th><th>Sport</th><th class='num'>Tickets</th><th class='num'>Hit%</th><th class='num'>Staked</th><th class='num'>P&amp;L</th><th class='num'>ROI</th><th class='num'>Bankroll</th><th class='num'>MaxDD</th><th>Last activity</th><th>Pricing</th></tr></thead><tbody>" + body + "</tbody></table></div>";
}

/* ---------------- Upcoming / Completed ---------------- */
function ticketFilters(prefix, strategies, sports) {
  return "<div class='filters'><select id='" + prefix + "-strat'><option value=''>All strategies</option>" +
    strategies.map((s) => "<option value='" + s + "'>" + s + " (" + esc(uname(s)) + ")</option>").join("") + "</select>" +
    "<select id='" + prefix + "-sport'><option value=''>All sports</option>" +
    sports.map((s) => "<option>" + s + "</option>").join("") + "</select>" +
    "<select id='" + prefix + "-grade'><option value=''>All grades</option><option>VERIFIED</option><option>REFERENCE</option><option>MODEL</option><option>MIXED</option><option>UNPRICED</option></select>" +
    "<select id='" + prefix + "-market'><option value=''>Any market</option><option value='ML'>Contains ML</option><option value='SPREAD'>Contains SPREAD</option><option value='TOTAL'>Contains TOTAL</option></select>" +
    "<select id='" + prefix + "-size'><option value=''>Any parlay size</option><option value='2'>2 legs</option><option value='3'>3 legs</option><option value='4'>4 legs</option><option value='5+'>5+ legs</option></select></div>";
}
function applyTicketFilters(list, prefix) {
  const fs = ($("#" + prefix + "-strat") || {}).value || "";
  const fsport = ($("#" + prefix + "-sport") || {}).value || "";
  const fg = ($("#" + prefix + "-grade") || {}).value || "";
  const fm = ($("#" + prefix + "-market") || {}).value || "";
  const fsz = ($("#" + prefix + "-size") || {}).value || "";
  return list.filter((p) => (!fs || p.strategy_id === fs) && (!fsport || (p.sports || []).indexOf(fsport) >= 0) && (!fg || p.pricing_grade === fg) &&
    (!fm || (p.market_mix || "").indexOf(fm) >= 0) &&
    (!fsz || (fsz === "5+" ? p.n_legs >= 5 : p.n_legs === Number(fsz))));
}
async function pageUpcoming() {
  const up = await get("upcoming");
  await users();
  const strats = [...new Set(up.map((p) => p.strategy_id))].sort();
  const sports = [...new Set(up.reduce((a, p) => a.concat(p.sports || []), []))].sort();
  const bySlate = {};
  up.forEach((p) => { (bySlate[p.slate_date] = bySlate[p.slate_date] || []).push(p); });
  const html = Object.keys(bySlate).sort().map((s) => "<h2>" + esc(s) + " <span class='mut'>(" + bySlate[s].length + " tickets)</span></h2>" +
    bySlate[s].map((p) => parlayCard(p)).join("")).join("");
  return "<h1>Upcoming</h1><p class='sub'><b>" + up.length + " tickets</b> awaiting settlement. Nothing here counts toward completed results until every leg is final. Filter, then expand any ticket for legs, odds basis and model prices.</p>" +
    ticketFilters("up", strats, sports) + "<div id='up-list'>" + html + "</div>";
}
async function pageCompleted() {
  const done = await get("completed");
  await users();
  window.__done = done;
  window.__pg = 0;
  const strats = [...new Set(done.map((p) => p.strategy_id))].sort();
  return "<h1>Completed</h1><p class='sub'><b>" + done.length + "</b> settled tickets (latest first). Settlement is append-only: results are never edited after the fact.</p>" +
    ticketFilters("done", strats, ["NFL", "MLB", "NHL", "NBA"]) +
    "<div class='filters'><select id='done-result'><option value=''>Any result</option><option>won</option><option>lost</option><option>push</option><option>void</option></select></div>" +
    "<div class='pager'><button id='pg-prev'>← Prev</button><span id='pg-info'></span><button id='pg-next'>Next →</button></div>" +
    "<div id='done-list'></div>";
}
function renderDonePage() {
  const all = window.__done || [];
  const res = ($("#done-result") || {}).value || "";
  let list = applyTicketFilters(all, "done");
  if (res) list = list.filter((p) => p.status === res);
  const per = 15;
  const pages = Math.max(1, Math.ceil(list.length / per));
  window.__pg = Math.min(Math.max(0, window.__pg || 0), pages - 1);
  const slice = list.slice(window.__pg * per, window.__pg * per + per);
  $("#done-list").innerHTML = slice.map((p) => parlayCard(p)).join("") || "<p class='mut'>No tickets match.</p>";
  $("#pg-info").textContent = "Page " + (window.__pg + 1) + " of " + pages + " · " + list.length + " tickets";
  $("#pg-prev").disabled = window.__pg === 0;
  $("#pg-next").disabled = window.__pg >= pages - 1;
}

/* ---------------- Strategies ---------------- */
async function pageStrategies() {
  const s = await get("strategies");
  const cards = s.map((r) => {
    const b = r.books.backtest, f = r.books.forward;
    return "<div class='card'><h3><a href='#/strategy/" + esc(r.strategy_id) + "'>" + esc(r.name) + "</a></h3>" +
      "<div class='sub'><code>" + esc(r.strategy_id) + "</code> · " + esc(r.username) + " · " + esc(r.sport) + " · " + esc(r.category) + " · " + esc(r.markets) + "</div>" +
      "<p><b>Hypothesis:</b> " + esc(r.hypothesis) + "</p>" +
      "<div class='kv'><span>Backtest</span><b class='" + cls(b.pnl) + "'>" + money(b.pnl) + " <span class='mut'>(" + b.parlays + " tickets)</span></b></div>" +
      "<div class='kv'><span>Forward</span><b class='" + cls(f.pnl) + "'>" + money(f.pnl) + " <span class='mut'>(" + f.parlays + " tickets)</span></b></div>" +
      "</div>";
  }).join("");
  return "<h1>Strategies</h1><p class='sub'>28 versioned hypotheses. Every rule, market and limitation is documented; every ticket links back to its strategy page.</p><div class='grid c2'>" + cards + "</div>";
}
async function pageStrategy(id) {
  const [s, hist] = await Promise.all([get("strategies"), get("history_" + id).catch(() => null)]);
  const r = s.find((x) => x.strategy_id === id);
  if (!r) return "<h1>Not found</h1><p>No strategy " + esc(id) + ".</p>";
  const bookTable = (b, label) => {
    const legs = b.legs || {};
    return "<h3>" + label + " book</h3><div class='grid c4'>" +
      "<div class='card'><div class='stat " + cls(b.pnl) + "'>" + money(b.pnl) + "</div><div class='mut'>P&amp;L on " + money(b.staked) + " staked</div></div>" +
      "<div class='card'><div class='stat'>" + pct(b.roi) + "</div><div class='mut'>ROI · " + b.parlays + " tickets (" + (b.won || 0) + "W/" + (b.lost || 0) + "L/" + (b.push || 0) + "P)</div></div>" +
      "<div class='card'><div class='stat'>" + money0(b.bankroll) + "</div><div class='mut'>bankroll (start " + money0(b.start) + ") · maxDD " + pct(b.max_drawdown) + "</div></div>" +
      "<div class='card'><div class='stat'>" + pct(b.parlay_hit_rate) + "</div><div class='mut'>ticket hit% · legs " + pct(b.leg_hit_rate) + "</div></div></div>" +
      "<div class='kv'><span>Avg legs</span><b>" + (b.avg_legs != null ? b.avg_legs : "—") + "</b></div>" +
      "<div class='kv'><span>Total payout</span><b>" + money(b.total_payout) + "</b></div>" +
      "<div class='kv'><span>Streak</span><b>" + esc(b.streak || "—") + "</b></div>" +
      "<div class='kv'><span>Verified-price share</span><b>" + pct(b.verified_share) + "</b></div>" +
      "<div class='kv'><span>Last activity</span><b>" + esc(b.last_activity || "—") + "</b></div>" +
      "<h3>" + label + " equity</h3>" + spark(b.equity) +
      "<h3>" + label + " legs by market</h3>" + splitTable((r.splits[label.toLowerCase()] || {}).by_market) +
      "<h3>" + label + " legs by sport</h3>" + splitTable((r.splits[label.toLowerCase()] || {}).by_sport);
  };
  let histHtml = "";
  if (hist && hist.tickets && hist.tickets.length) {
    const fwd = hist.tickets.filter((p) => p.status === "upcoming" || p.status === "live");
    const fin = hist.tickets.filter((p) => !(p.status === "upcoming" || p.status === "live"))
      .sort((a, b) => (a.slate_date < b.slate_date ? 1 : -1));
    window.__hist = { fwd: fwd, fin: fin, pg: 0 };
    histHtml = "<h2>Complete ticket history (" + hist.tickets.length + ")</h2>" +
      "<p class='sub'>Every parlay this strategy has ever filed — upcoming first, then the full completed record (paginated 10 at a time).</p>" +
      (fwd.length ? "<h3>Upcoming / live (" + fwd.length + ")</h3>" + fwd.map((p) => parlayCard(p)).join("") : "<p class='mut'>No open tickets.</p>") +
      "<h3>Completed (" + fin.length + ")</h3><div class='pager'><button id='h-pg-prev'>← Prev</button><span id='h-pg-info'></span><button id='h-pg-next'>Next →</button></div><div id='h-list'></div>";
  } else {
    histHtml = "<h2>Complete ticket history</h2><p class='mut'>History file unavailable — see the books above for aggregates.</p>";
  }
  return "<h1>" + esc(r.name) + "</h1><p class='sub'><code>" + esc(r.strategy_id) + "</code> " + esc(r.version) + " · managed by <b>" + esc(r.username) + "</b> · " + esc(r.sport) + " · " + esc(r.category) + " · status " + esc(r.status) + "</p>" +
    "<div class='card'><h3>Hypothesis</h3><p>" + esc(r.hypothesis) + "</p>" +
    "<div class='kv'><span>Markets</span><b>" + esc(r.markets) + "</b></div>" +
    "<div class='kv'><span>Selection rules</span><b>" + esc(r.selection_rules) + "</b></div>" +
    "<div class='kv'><span>Construction</span><b>" + esc(r.construction_rules) + "</b></div>" +
    "<div class='kv'><span>Required data</span><b>" + esc(r.required_data) + "</b></div>" +
    "<div class='kv'><span>Min edge</span><b>" + r.min_edge + "</b></div>" +
    "<div class='kv'><span>Stake</span><b>" + money(r.stake) + "</b></div>" +
    "<div class='kv'><span>Max legs</span><b>" + r.max_legs + "</b></div>" +
    (r.lineage ? "<div class='kv'><span>Lineage</span><b>" + esc(r.lineage) + "</b></div>" : "") +
    "<div class='callout'><b>Limitations:</b> " + esc(r.limitations) + "</div></div>" +
    bookTable(r.books.backtest, "Backtest") + bookTable(r.books.forward, "Forward") + histHtml;
}
function renderHistPage() {
  const h = window.__hist;
  if (!h || !$("#h-list")) return;
  const per = 10, pages = Math.max(1, Math.ceil(h.fin.length / per));
  h.pg = Math.min(Math.max(0, h.pg || 0), pages - 1);
  $("#h-list").innerHTML = h.fin.slice(h.pg * per, h.pg * per + per).map((p) => parlayCard(p)).join("") || "<p class='mut'>No completed tickets.</p>";
  $("#h-pg-info").textContent = "Page " + (h.pg + 1) + " of " + pages;
  $("#h-pg-prev").disabled = h.pg === 0;
  $("#h-pg-next").disabled = h.pg >= pages - 1;
}
function splitTable(sp) {
  if (!sp || !Object.keys(sp).length) return "<p class='mut'>No settled legs yet.</p>";
  return "<table class='data'><thead><tr><th>Slice</th><th class='num'>Won</th><th class='num'>Lost</th><th class='num'>Hit%</th></tr></thead><tbody>" +
    Object.keys(sp).sort().map((k) => "<tr><td>" + esc(k) + "</td><td class='num'>" + sp[k].win + "</td><td class='num'>" + sp[k].loss + "</td><td class='num'>" + pct(sp[k].hit) + "</td></tr>").join("") + "</tbody></table>";
}

/* ---------------- Sport pages ---------------- */
async function pageSport(sp) {
  const g = await get("games");
  const d = (g.by_sport || {})[sp];
  if (!d) return "<h1>No data</h1>";
  const priceTxt = (t) => (t.prices || []).map((p) => p.market + " " + p.selection + (p.line != null ? " " + p.line : "") + " " + (p.odds_american > 0 ? "+" : "") + Math.round(p.odds_american)).join(" · ");
  const mkUp = (t) => "<tr><td>" + esc(t.game_date) + "</td><td><b>" + esc(t.away_team) + " @ " + esc(t.home_team) + "</b><br><span class='mut'>" + esc(t.game_key || "") + "</span></td><td><span class='mut'>" + esc(priceTxt(t) || "no lines yet") + "</span></td><td>scheduled</td><td><span class='mut'>" + esc(t.source_id || "") + "</span></td></tr>";
  const mkFin = (t) => "<tr><td>" + esc(t.game_date) + "</td><td><b>" + esc(t.away_team) + " @ " + esc(t.home_team) + "</b><br><span class='mut'>" + esc(t.game_key || "") + "</span></td><td class='num'>" + t.away_score + "–" + t.home_score + "</td><td>final</td><td><span class='mut'>" + esc(t.source_id || "") + "</span></td></tr>";
  const form = (d.form_snapshot || []).map((t) => "<tr><td><b>" + esc(t.team) + "</b></td><td class='num'>" + t.w + "–" + t.l + "</td><td class='num'>" + (t.last10_w != null ? t.last10_w + "–" + t.last10_l : "—") + "</td><td>" + esc(t.streak_code || "—") + "</td><td class='num'>" + (t.rs != null ? t.rs + ":" + t.ra : "—") + "</td></tr>").join("");
  return "<h1>" + esc(sp) + "</h1><p class='sub'>Schedules, results and standings snapshots with per-row sources. Odds lines live on the tickets that used them (see Upcoming / Completed).</p>" +
    (form ? "<h2>Standings snapshot</h2><div style='overflow-x:auto'><table class='data'><thead><tr><th>Team</th><th class='num'>W–L</th><th class='num'>L10</th><th>Streak</th><th class='num'>RS:RA</th></tr></thead><tbody>" + form + "</tbody></table></div>" : "") +
    "<h2>Upcoming (" + (d.upcoming || []).length + ")</h2><div style='overflow-x:auto'><table class='data'><thead><tr><th>Date</th><th>Matchup</th><th>Lines</th><th>Status</th><th>Source</th></tr></thead><tbody>" + (d.upcoming || []).map(mkUp).join("") + "</tbody></table></div>" +
    "<h2>Recent finals (" + (d.recent_finals || []).length + ")</h2><div style='overflow-x:auto'><table class='data'><thead><tr><th>Date</th><th>Matchup</th><th class='num'>Score</th><th>Status</th><th>Source</th></tr></thead><tbody>" + (d.recent_finals || []).map(mkFin).join("") + "</tbody></table></div>";
}

/* ---------------- Performance ---------------- */
async function pagePerformance() {
  const [perf, lbb, lbf] = await Promise.all([get("performance"), get("leaderboard_backtest"), get("leaderboard_forward")]);
  await users();
  const eq = perf.equity || {};
  const keys = Object.keys(eq).sort();
  const cards = keys.slice(0, 28).map((k) => {
    const sid = k.split("|")[0];
    const e = eq[k] || {};
    return "<div class='card'><h3>" + stratLink(sid) + " <span class='mut'>" + esc(uname(sid)) + "</span></h3>" +
      "<div class='sub'>backtest</div>" + spark(e.backtest) + "<div class='sub'>forward</div>" + spark(e.forward) + "</div>";
  }).join("");
  const daily = (perf.daily_pnl || []).slice(-30).reverse().map((d) =>
    "<tr><td>" + esc(d.slate_date) + "</td><td>" + esc(d.test_mode) + "</td><td class='num'>" + d.n + "</td><td class='num " + cls(d.pnl) + "'>" + money(d.pnl) + "</td></tr>").join("");
  const splitRows = (sp, labelFn) => Object.keys(sp || {}).sort().map((k) => {
    const d = sp[k] || {};
    return "<tr><td>" + labelFn(k) + "</td><td class='num'>" + (d.parlays || 0) + "</td><td class='num'>" + (d.won || 0) + "W/" + (d.lost || 0) + "L/" + (d.push || 0) + "P</td>" +
      "<td class='num'>" + (d.leg_win || 0) + "/" + ((d.leg_win || 0) + (d.leg_loss || 0)) + "</td><td class='num'>" + pct(d.leg_hit) + "</td>" +
      "<td class='num " + cls(d.pnl) + "'>" + money(d.pnl) + "</td><td class='num'>" + pct(d.roi) + "</td></tr>";
  }).join("");
  const head = "<thead><tr><th>Slice</th><th class='num'>Tickets</th><th class='num'>W/L/P</th><th class='num'>Legs won</th><th class='num'>Leg hit%</th><th class='num'>P&amp;L</th><th class='num'>ROI</th></tr></thead>";
  return "<h1>Performance</h1><p class='sub'>Per-strategy equity curves (one spark per sealed book) and competition-wide splits by sport, market and strategy. Drawdowns and losers are shown, not hidden.</p>" +
    "<h2>By sport</h2><table class='data'>" + head + "<tbody>" + splitRows(perf.by_sport, (k) => esc(k)) + "</tbody></table>" +
    "<h2>By market / market mix</h2><table class='data'>" + head + "<tbody>" + splitRows(perf.by_market, (k) => esc(k)) + "</tbody></table>" +
    "<h2>By strategy</h2><table class='data'>" + head + "<tbody>" + splitRows(perf.by_strategy, (k) => stratLink(k)) + "</tbody></table>" +
    "<h2>Equity curves</h2><div class='grid c2'>" + cards + "</div>" +
    "<h2>Daily P&amp;L (last 30 days with action)</h2><table class='data'><thead><tr><th>Date</th><th>Book</th><th class='num'>Tickets</th><th class='num'>P&amp;L</th></tr></thead><tbody>" + daily + "</tbody></table>";
}

/* ---------------- Research / Sources / Quality ---------------- */
async function pageResearch() {
  const r = await get("research");
  const cards = r.map((x) => "<div class='card'><h3>" + esc(x.title) + " <span class='mut'>(" + esc(x.research_id) + " · " + esc(x.sport) + " · " + esc(x.status) + ")</span></h3>" +
    "<p><b>Hypothesis:</b> " + esc(x.hypothesis) + "</p><p><b>Method:</b> " + esc(x.method) + "</p>" +
    "<p><b>Window:</b> " + esc(x.data_window) + "</p><p><b>Result:</b> " + esc(x.result) + "</p>" +
    "<div class='sub'>Logged " + esc(dt(x.created_utc)) + (x.ref_strategy ? " · feeds " + stratLink(x.ref_strategy) : "") + "</div></div>").join("");
  return "<h1>Research log</h1><p class='sub'>Dated hypotheses, methods and outcomes — including negative results. Strategies cite the entries they came from.</p><div class='grid c2'>" + cards + "</div>";
}
async function pageSources() {
  const s = await get("sources");
  const rows = s.map((x) => "<tr><td><b>" + esc(x.name) + "</b><br><code>" + esc(x.source_id) + "</code></td>" +
    "<td><span class='mono'>" + esc(x.url) + "</span><br><span class='mut'>" + esc(x.access_method) + " · " + esc(x.cost) + "</span></td>" +
    "<td>" + esc(x.data_type) + "</td><td>" + esc(x.historical_depth) + "</td><td>" + esc(x.last_verified_utc) + "<br><b>" + esc(x.status) + "</b></td><td><span class='mut'>" + esc(x.limitations) + "</span></td></tr>").join("");
  return "<h1>Data sources</h1><p class='sub'>Every fact on this site traces to one of these free public sources. No paid feeds, no scraped book odds presented as fact, no invented lines.</p>" +
    "<div style='overflow-x:auto'><table class='data'><thead><tr><th>Source</th><th>Access</th><th>Data</th><th>History</th><th>Verified</th><th>Limits</th></tr></thead><tbody>" + rows + "</tbody></table></div>";
}
async function pageQuality() {
  const q = await get("quality");
  const iss = (q.issues || []).map((i) => "<div class='card issue-" + (i.severity === "error" ? "err callout" : "callout") + "'><b>#" + i.issue_id + " [" + esc(i.severity) + "] " + esc(i.area) + "</b> — " + esc(i.detail) +
    "<br><span class='mut'>" + esc(i.created_utc) + " · status: " + esc(i.status) + (i.resolution ? " · resolution: " + esc(i.resolution) : "") + "</span></div>").join("");
  const ver = (q.verifications || []).map((v) => "<tr><td>" + esc(v.subject) + "</td><td>" + esc(v.claim) + "</td><td><span class='mono'>" + esc(v.source_url) + "</span></td><td><b>" + esc(v.result) + "</b></td><td><span class='mut'>" + esc(v.detail) + "</span></td></tr>").join("");
  const checks = ((q.last_checks || {}).checks || []).map((c) => "<tr><td><code>" + esc(c.check) + "</code></td><td class='num'>" + c.problems + "</td><td><span class='mut'>" + esc((c.detail || []).join(" | ")) + "</span></td></tr>").join("");
  return "<h1>Data quality</h1><p class='sub'>The pipeline flags problems instead of guessing. Open issues, independent verifications and the last full scan live here.</p>" +
    "<h2>Open issues (" + (q.open || 0) + ")</h2>" + (iss || "<p class='mut'>None open.</p>") +
    "<h2>Independent verifications (" + (q.verifications || []).length + ")</h2>" +
    "<div style='overflow-x:auto'><table class='data'><thead><tr><th>Subject</th><th>Claim</th><th>Source</th><th>Result</th><th>Detail</th></tr></thead><tbody>" + ver + "</tbody></table></div>" +
    "<h2>Last full scan</h2><table class='data'><thead><tr><th>Check</th><th class='num'>Problems</th><th>Detail</th></tr></thead><tbody>" + checks + "</tbody></table>";
}

/* ---------------- router ---------------- */
const ROUTES = [
  [/^dashboard$/, pageDashboard],
  [/^leaderboard$/, pageLeaderboard],
  [/^upcoming$/, pageUpcoming],
  [/^completed$/, pageCompleted],
  [/^strategies$/, pageStrategies],
  [/^strategy\/(.+)$/, pageStrategy],
  [/^sport\/(NFL|MLB|NHL|NBA)$/, pageSport],
  [/^performance$/, pagePerformance],
  [/^research$/, pageResearch],
  [/^sources$/, pageSources],
  [/^quality$/, pageQuality],
];
async function render() {
  const h = (location.hash || "#/dashboard").replace(/^#\//, "");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.r === h.split("?")[0]));
  const app = $("#app");
  try {
    for (const [re, fn] of ROUTES) {
      const m = h.match(re);
      if (m) {
        app.innerHTML = '<p class="mut">Loading…</p>';
        app.innerHTML = await fn(m[1]);
        afterRender(h);
        window.scrollTo(0, 0);
        return;
      }
    }
    app.innerHTML = "<h1>404</h1><p>Unknown page. <a href='#/dashboard'>Dashboard</a></p>";
  } catch (e) {
    app.innerHTML = "<h1>Load error</h1><p class='mut'>" + esc(e.message) + "</p>";
  }
}
function afterRender(h) {
  if (h === "leaderboard") {
    document.querySelectorAll("[data-book]").forEach((b) => b.addEventListener("click", () => { window.__lb.book = b.dataset.book; render(); }));
    document.querySelectorAll("[data-sport]").forEach((b) => b.addEventListener("click", () => { window.__lb.sport = b.dataset.sport; render(); }));
    ["from", "to", "strat", "market", "size"].forEach((k) => {
      const el = $("#lb-" + k);
      if (el) el.addEventListener("change", () => { window.__lb[k] = el.value; render(); });
    });
    const clr = $("#lb-clear");
    if (clr) clr.addEventListener("click", () => { Object.assign(window.__lb, { from: "", to: "", strat: "", market: "", size: "" }); render(); });
  }
  if (h === "upcoming") {
    ["up-strat", "up-sport", "up-grade", "up-market", "up-size"].forEach((id) => on("#" + id, "change", async () => {
      const up = await get("upcoming");
      const list = applyTicketFilters(up, "up");
      const bySlate = {};
      list.forEach((p) => { (bySlate[p.slate_date] = bySlate[p.slate_date] || []).push(p); });
      $("#up-list").innerHTML = Object.keys(bySlate).sort().map((s) => "<h2>" + esc(s) + " <span class='mut'>(" + bySlate[s].length + ")</span></h2>" + bySlate[s].map((p) => parlayCard(p)).join("")).join("") || "<p class='mut'>No tickets match.</p>";
    }));
  }
  if (h === "completed") {
    renderDonePage();
    ["done-strat", "done-sport", "done-grade", "done-result", "done-market", "done-size"].forEach((id) => on("#" + id, "change", () => { window.__pg = 0; renderDonePage(); }));
    on("#pg-prev", "click", () => { window.__pg--; renderDonePage(); });
    on("#pg-next", "click", () => { window.__pg++; renderDonePage(); });
  }
  if (h.indexOf("strategy/") === 0) {
    renderHistPage();
    const prev = $("#h-pg-prev"), next = $("#h-pg-next");
    if (prev) prev.addEventListener("click", () => { window.__hist.pg--; renderHistPage(); });
    if (next) next.addEventListener("click", () => { window.__hist.pg++; renderHistPage(); });
  }
}
async function boot() {
  try {
    const meta = await get("meta");
    $("#asof").textContent = "data as of " + (meta.data_as_of_utc || "?").replace("T", " ").replace("Z", " UTC");
    $("#foot").innerHTML = "ParlaySports · simulated paper competition · no real money · engine v" +
      esc(meta.engine_version || "?") + " · seed manifest <code>" +
      esc((meta.seed_manifest_sha256 || "").slice(0, 12)) + "</code> · exported " + esc(dt(meta.exported_utc));
  } catch (e) { /* offline preview */ }
  window.addEventListener("hashchange", render);
  render();
}
boot();
