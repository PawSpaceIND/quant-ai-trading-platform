import { test, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { NextRequest } from "next/server";
import { dailyBudget } from "../lib/console-db";
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pramana-chat-budget-"));
process.env.PRAMANA_TENANT_ID = "default";
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "ledger.sqlite");
process.env.PRAMANA_CONSOLE_DB = path.join(dir, "console.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT = path.join(dir, "market.json");
process.env.PRAMANA_PROOF_DIR = path.join(dir, "proofs");
process.env.PRAMANA_CHAT_DAILY_LIMIT = "2";
// No key: generateAnswer stores an error row without any paid call.
delete process.env.ANTHROPIC_API_KEY;
after(() => fs.rmSync(dir, { recursive: true, force: true }));
const ask = (POST: (req: NextRequest) => Promise<Response>, prompt: string) =>
  POST(
    new NextRequest("http://localhost/api/copilot", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ prompt }),
    }),
  );
// Earlier tests in this file spend the per-minute copilot rate limit, which would refuse
// a later request before it ever reaches the gate under test.
const clearRateLimit = () => {
  const db = new DatabaseSync(process.env.PRAMANA_CONSOLE_DB!);
  try { db.prepare("DELETE FROM limits WHERE key LIKE 'copilot:%'").run(); } finally { db.close(); }
};
const audits = () => {
  const db = new DatabaseSync(process.env.PRAMANA_CONSOLE_DB!);
  try {
    return (
      db
        .prepare("SELECT COUNT(*) AS n FROM audit WHERE action='copilot.budget_exhausted'")
        .get() as { n: number }
    ).n;
  } finally {
    db.close();
  }
};
test("daily budget counts attempts per UTC day and a peek never consumes", () => {
  assert.deepEqual(dailyBudget("unit", 2, false), { allowed: true, used: 0, remaining: 2 });
  assert.deepEqual(dailyBudget("unit", 2), { allowed: true, used: 1, remaining: 1 });
  assert.deepEqual(dailyBudget("unit", 2), { allowed: true, used: 2, remaining: 0 });
  assert.deepEqual(dailyBudget("unit", 2, false), { allowed: false, used: 2, remaining: 0 });
  assert.deepEqual(dailyBudget("unit", 2), { allowed: false, used: 3, remaining: 0 });
  assert.equal(dailyBudget("other", 2).allowed, true);
});
test("copilot POST is allowed until the daily limit, then refused with one audit row", async () => {
  const { GET, POST } = await import("../app/api/copilot/route");
  let status = await (await GET()).json();
  assert.equal(status.dailyLimit, 2);
  assert.equal(status.dailyRemaining, 2);
  assert.equal((await ask(POST, "first")).status, 200);
  assert.equal((await ask(POST, "second")).status, 200);
  status = await (await GET()).json();
  assert.equal(status.dailyRemaining, 0);
  assert.equal(status.conversations.length, 2);
  for (const prompt of ["third", "fourth"]) {
    const refused = await ask(POST, prompt);
    assert.equal(refused.status, 429);
    assert.equal(
      (await refused.json()).error,
      "Daily Atlas chat budget reached (2). Resets at 00:00 UTC.",
    );
  }
  assert.equal((await (await GET()).json()).conversations.length, 2);
  assert.equal(audits(), 1);
});
test("a limit of zero or less disables the daily cap", async () => {
  process.env.PRAMANA_CHAT_DAILY_LIMIT = "0";
  const { GET, POST } = await import("../app/api/copilot/route");
  const status = await (await GET()).json();
  assert.equal(status.dailyLimit, null);
  assert.equal(status.dailyRemaining, null);
  assert.equal((await ask(POST, "fifth")).status, 200);
});

test("the shared dollar guard refuses paid calls only, never a free unconfigured answer", async () => {
  // The guard was consulted for every request. With no key configured the answer is a
  // locally generated refusal that saves the question and its evidence and cannot spend a
  // cent, so an activation hold silently disabled the copilot on any deployment without a
  // key - including the container smoke, where it returned a budget error in place of the
  // saved row the operator is meant to get.
  const previous = {
    limit: process.env.PRAMANA_AI_DAILY_USD_LIMIT,
    db: process.env.PRAMANA_AI_SPEND_DB,
    key: process.env.ANTHROPIC_API_KEY,
    daily: process.env.PRAMANA_CHAT_DAILY_LIMIT,
  };
  try {
    process.env.PRAMANA_AI_DAILY_USD_LIMIT = "2.50";
    process.env.PRAMANA_AI_SPEND_DB = path.join(dir, "absent-ai-spend.sqlite");
    process.env.PRAMANA_CHAT_DAILY_LIMIT = "50";
    delete process.env.ANTHROPIC_API_KEY;
    const { GET, POST } = await import("../app/api/copilot/route");
    // The guard is on an activation hold and the panel still reports it.
    assert.equal((await (await GET()).json()).dollarBudget.status, "activation_hold");
    const before = (await (await GET()).json()).conversations.length;
    clearRateLimit();
    const answered = await ask(POST, "unconfigured question during an activation hold");
    assert.equal(answered.status, 200);
    const row = await answered.json();
    assert.equal(row.status, "error");
    assert.match(row.error, /not configured/i);
    assert(row.context, "the question's evidence context must still be saved");
    assert.equal((await (await GET()).json()).conversations.length, before + 1);

    // With a key the same hold refuses before a daily question is consumed.
    process.env.ANTHROPIC_API_KEY = "synthetic-no-network-key";
    const remainingBefore = (await (await GET()).json()).dailyRemaining;
    clearRateLimit();
    const refused = await ask(POST, "paid question during an activation hold");
    assert.equal(refused.status, 503);
    assert.match((await refused.json()).error, /paused until 05:30 IST/);
    assert.equal((await (await GET()).json()).dailyRemaining, remainingBefore);
  } finally {
    for (const [name, value] of [["PRAMANA_AI_DAILY_USD_LIMIT", previous.limit], ["PRAMANA_AI_SPEND_DB", previous.db],
                                 ["ANTHROPIC_API_KEY", previous.key], ["PRAMANA_CHAT_DAILY_LIMIT", previous.daily]] as const)
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
  }
});
