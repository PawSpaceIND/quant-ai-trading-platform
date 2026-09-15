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
