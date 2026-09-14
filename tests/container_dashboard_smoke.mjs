// Runs inside the isolated dashboard container against its real npm-start server.
import assert from "node:assert/strict";

const origin = process.env.SMOKE_ORIGIN || "http://localhost:3000";
assert.ok(["localhost", "127.0.0.1"].includes(new URL(origin).hostname));
const phase = process.env.SMOKE_PHASE;
const anonymous = await fetch(`${origin}/api/workspace`);
assert.equal(anonymous.status, 401);
const login = await fetch(`${origin}/api/session`, {
  method: "POST", headers: {origin, "Content-Type": "application/json"},
  body: JSON.stringify({secret: process.env.PRAMANA_DASHBOARD_SECRET}),
});
assert.equal(login.status, 200);
const cookie = login.headers.get("set-cookie")?.split(";")[0];
assert.ok(cookie?.startsWith("pramana_session="));
async function request(route, method = "GET", body, requestOrigin = origin) {
  return fetch(origin + route, {method, headers: {cookie, origin: requestOrigin, "Content-Type": "application/json"},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})});
}
const response = await request("/api/workspace");
assert.equal(response.status, 200);
assert.match(response.headers.get("cache-control"), /no-store/);
const workspace = await response.json();
assert.equal(workspace.tenantId, "pilot");
assert.equal(workspace.liveEnabled, false);
assert.equal(workspace.copilotConfigured, false);
assert.equal(workspace.runtime.mode, "paper");
assert.equal(workspace.checks.find(c => c.id === "recovery").pass, false);
assert.equal(workspace.checks.find(c => c.id === "evidence").pass, false);
assert.equal(workspace.portfolio.holdings.length, 1);
assert.equal(workspace.portfolio.holdings[0].symbol, "INFY");
assert.equal(workspace.portfolio.holdings[0].quantity, 2);
if (phase === "stale") {
  assert.equal(workspace.runtime.status, "stale");
  assert.equal(workspace.checks.find(c => c.id === "engine").pass, false);
  assert.equal(workspace.checks.find(c => c.id === "entry_controls").pass, false);
} else {
  assert.equal(workspace.runtime.status, "running");
  assert.equal(workspace.runtime.halted, phase === "halted");
  assert.equal(workspace.checks.find(c => c.id === "entry_controls").pass, phase !== "halted");
  assert.equal(workspace.portfolio.status, "ok");
  assert.equal(workspace.portfolio.holdings[0].markPrice, 100);
  assert.equal(workspace.market.rows[0].symbol, "INFY");
}
if (phase === "initial") {
  assert.equal((await request("/api/watchlist", "PUT", {symbols:["INFY"]}, "https://wrong.invalid")).status, 403);
  assert.equal((await request("/api/watchlist", "PUT", {symbols:["INFY"]})).status, 200);
  assert.equal((await request("/api/control", "POST", {action:"resume", reason:"unsupported"})).status, 400);
}
assert.deepEqual(await (await request("/api/watchlist")).json(), {symbols:["INFY"]});
if (phase === "initial") {
  const halt = await request("/api/control", "POST", {action:"halt", reason:"Isolated container smoke drill"});
  assert.equal(halt.status, 200);
  assert.equal((await halt.json()).status, "requested");
}
console.log(JSON.stringify({phase, status:"pass", authenticated:true, liveEnabled:false,
  runtime:workspace.runtime.status, halted:workspace.runtime.halted, savedWatchlist:["INFY"],
  accountQuantity:2, operatorAcceptance:false}));
