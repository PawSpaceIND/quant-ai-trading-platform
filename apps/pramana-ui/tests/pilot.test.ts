import { test, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { NextRequest } from "next/server";
import {
  makeSession,
  validSession,
  validOrigin,
  boundedJson,
} from "../lib/auth";
import { readLivePortfolio, performance } from "../lib/pilot";
import { generateAnswer, conversations } from "../lib/copilot";
import { rateLimit } from "../lib/console-db";
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pramana-ui-test-"));
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "ledger.sqlite");
process.env.PRAMANA_CONSOLE_DB = path.join(dir, "console.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT = path.join(dir, "market.json");
process.env.PRAMANA_DASHBOARD_SECRET = "test-key-32-characters-minimum-only";
after(() => fs.rmSync(dir, { recursive: true, force: true }));
test("signed sessions reject tampering, extra fields and secret rotation", () => {
  const s = makeSession();
  assert(validSession(s));
  assert(!validSession(s + ".extra"));
  assert(!validSession("0." + s.split(".")[1]));
  assert(!validSession(s.slice(0, -1) + "z"));
  process.env.PRAMANA_DASHBOARD_SECRET += "rotate";
  assert(!validSession(s));
});
test("unsafe requests need exact origin and bounded JSON", async () => {
  assert(
    validOrigin(
      new NextRequest("https://pilot.test/api/control", {
        headers: { origin: "https://pilot.test" },
      }),
    ),
  );
  assert(
    !validOrigin(
      new NextRequest("https://pilot.test/api/control", {
        headers: { origin: "https://evil.test" },
      }),
    ),
  );
  assert(!validOrigin(new NextRequest("https://pilot.test/api/control")));
  await assert.rejects(() =>
    boundedJson(
      new Request("http://a", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ x: "a".repeat(100) }),
      }),
      20,
    ),
  );
});
test("rate counters persist between connections", () => {
  assert(rateLimit("test", 2));
  assert(rateLimit("test", 2));
  assert(!rateLimit("test", 2));
});
test("new fills invalidate old snapshots and partial days are not performance evidence", () => {
  const db = new DatabaseSync(process.env.PRAMANA_LEDGER_PATH!);
  db.exec(
    "CREATE TABLE paper_ledger(id INTEGER,tenant_id TEXT); CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,ledger_id INTEGER,payload TEXT)",
  );
  const payload = {
    updatedAt: new Date().toISOString(),
    allMarksFresh: true,
    status: "ok",
    totalEquity: 100000,
    startingCapital: 100000,
    qualifyingSession: true,
    sessionDate: "2026-01-05",
  };
  db.prepare("INSERT INTO paper_live_valuations VALUES ('default',?,0,?)").run(
    payload.updatedAt,
    JSON.stringify(payload),
  );
  assert.equal(readLivePortfolio()?.status, "ok");
  assert.equal(performance().days, 0);
  db.exec("INSERT INTO paper_ledger VALUES (1,'default')");
  assert.equal(readLivePortfolio()?.status, "stale");
  db.close();
});
test("copilot persists unavailable-provider errors with addressable context", async () => {
  delete process.env.ANTHROPIC_API_KEY;
  const r = await generateAnswer("Explain my risk");
  assert.equal(r.status, "error");
  assert.match(r.error!, /not configured/);
  assert(conversations(r.id)[0].context);
});
test("copilot saves response and usage; retry does not charge again", async () => {
  process.env.ANTHROPIC_API_KEY = "synthetic-test-key";
  let calls = 0;
  const transport: typeof fetch = async () => {
    calls++;
    return new Response(
      JSON.stringify({
        content: [{ type: "text", text: "Synthetic QA answer. No actions." }],
        usage: { input_tokens: 20, output_tokens: 8 },
      }),
      { status: 200 },
    );
  };
  const r = await generateAnswer(
    "Explain stale prices",
    undefined,
    undefined,
    transport,
  );
  assert.equal(r.status, "complete");
  assert.equal(JSON.parse(r.usage!).inputTokens, 20);
  assert.equal(conversations(r.id)[0].answer, r.answer);
  await generateAnswer("retry", undefined, r.id, transport);
  assert.equal(calls, 1);
});

test("malformed market records cannot break the dashboard", async () => {
  const { readMarket } = await import("../lib/market");
  fs.writeFileSync(
    process.env.PRAMANA_MARKET_SNAPSHOT!,
    JSON.stringify({
      status: "ok",
      fetchedAt: new Date().toISOString(),
      rows: [
        { symbol: 42, available: true, price: 100 },
        { symbol: "INFY", available: true, price: 1500, history: [] },
      ],
    }),
  );
  const result = await readMarket();
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].symbol, "INFY");
});

test("interrupted copilot requests become visible errors after restart", async () => {
  const { consoleDb } = await import("../lib/console-db");
  const db = consoleDb();
  db.prepare(
    "INSERT INTO conversations(id,tenant,prompt,status,created_at) VALUES (?,?,?,?,?)",
  ).run("interrupted", "default", "test", "pending", "2000-01-01T00:00:00Z");
  db.close();
  const record = conversations("interrupted")[0];
  assert.equal(record.status, "error");
  assert.match(record.error!, /billing may be unknown/);
});

test("account metrics need complete consecutive sessions and after-cost equity", () => {
  const db = new DatabaseSync(process.env.PRAMANA_LEDGER_PATH!);
  db.exec("DELETE FROM paper_live_valuations");
  const insert = db.prepare(
    "INSERT INTO paper_live_valuations VALUES ('default',?,1,?)",
  );
  let equity = 100000,
    previousSessionDate = "1999-12-31";
  for (let day = 0; day < 21; day++) {
    if (day) equity *= day % 2 ? 0.99 : 1.02;
    const date = new Date(Date.UTC(2000, 0, 3 + day))
      .toISOString()
      .slice(0, 10);
    for (let minute = 0; minute < 301; minute++) {
      const timestamp = new Date(
        Date.parse(date + "T04:55:00Z") + minute * 60000,
      ).toISOString();
      insert.run(
        timestamp,
        JSON.stringify({
          updatedAt: timestamp,
          sessionDate: date,
          previousSessionDate,
          qualifyingSession: true,
          allMarksFresh: true,
          startingCapital: 100000,
          totalEquity: equity,
        }),
      );
    }
    previousSessionDate = date;
  }
  const result = performance();
  assert.equal(result.days, 21);
  assert.equal(result.status, "observed");
  assert(result.sharpe! > 5 && result.sharpe! < 5.2);
  assert(Math.abs(result.netReturn! - (Math.pow(0.99 * 1.02, 10) - 1)) < 1e-10);
  db.prepare("DELETE FROM paper_live_valuations WHERE timestamp LIKE ?").run(
    "2000-01-12%",
  );
  assert.equal(performance().sharpe, null);
  db.close();
});

test("operator reviews reject tampering, expiration and a different release", async () => {
  const { reviewedGate } = await import("../lib/review");
  const { createHmac } = await import("node:crypto");
  process.env.PRAMANA_REVIEW_DIR = path.join(dir, "reviews");
  fs.mkdirSync(process.env.PRAMANA_REVIEW_DIR);
  process.env.PRAMANA_REVIEW_SECRET = "test-review-key-never-used-outside-qa";
  process.env.PRAMANA_RELEASE_REVISION = "a".repeat(40);
  const payload = JSON.stringify({
    schema: "pramana.pilot.acceptance.v1",
    scope: "private-paper-pilot",
    gate: "recovery",
    tenant_id: "default",
    release_revision: "a".repeat(40),
    reviewer: "Synthetic QA",
    reviewed_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86400000).toISOString(),
  });
  const signature = createHmac("sha256", process.env.PRAMANA_REVIEW_SECRET)
    .update(payload)
    .digest("hex");
  const file = path.join(process.env.PRAMANA_REVIEW_DIR, "recovery.json");
  fs.writeFileSync(file, JSON.stringify({ payload, signature }));
  assert(reviewedGate("recovery").pass);
  process.env.PRAMANA_RELEASE_REVISION = "b".repeat(40);
  assert(!reviewedGate("recovery").pass);
  process.env.PRAMANA_RELEASE_REVISION = "a".repeat(40);
  fs.writeFileSync(
    file,
    JSON.stringify({
      payload: payload.replace("Synthetic QA", "Changed"),
      signature,
    }),
  );
  assert(!reviewedGate("recovery").pass);
  const expired = JSON.stringify({
    ...JSON.parse(payload),
    reviewed_at: "2000-01-01T00:00:00Z",
    expires_at: "2000-01-02T00:00:00Z",
  });
  fs.writeFileSync(
    file,
    JSON.stringify({
      payload: expired,
      signature: createHmac("sha256", process.env.PRAMANA_REVIEW_SECRET)
        .update(expired)
        .digest("hex"),
    }),
  );
  assert(!reviewedGate("recovery").pass);
});

test("protective evidence is tenant-scoped, exact-order linked and never swarm analysis", async () => {
  const { proofsByOrderId, latestSwarmIntelligence } = await import("../lib/proofs");
  process.env.PRAMANA_PROOF_DIR = path.join(dir, "absent-proof-directory");
  const db = new DatabaseSync(process.env.PRAMANA_LEDGER_PATH!);
  db.exec("CREATE TABLE IF NOT EXISTS paper_protection_evidence(order_id TEXT PRIMARY KEY,tenant_id TEXT,payload TEXT)");
  const proof = {schema:"pramana.protective_exit.v1",event_type:"protective_exit",tenant_id:"default",order_id:"PAPER-PROTECTED-QA",declared_rationales:["Deterministic stop; no AI vote"],risk_verdict:{approved:"true"},stress_verdict:{passed:"not_applicable"}};
  const insert = db.prepare("INSERT INTO paper_protection_evidence VALUES(?,?,?)");
  insert.run(proof.order_id,"default",JSON.stringify(proof));
  insert.run("PAPER-OTHER-QA","other",JSON.stringify({...proof,order_id:"PAPER-OTHER-QA",tenant_id:"other"}));
  insert.run("PAPER-MISMATCH-QA","default",JSON.stringify(proof));
  db.close();
  const index = proofsByOrderId();
  assert.equal(index.get(proof.order_id)?.proof.event_type,"protective_exit");
  assert(!index.has("PAPER-OTHER-QA"));
  assert(!index.has("PAPER-MISMATCH-QA"));
  assert.equal(latestSwarmIntelligence().status,"empty");
});
