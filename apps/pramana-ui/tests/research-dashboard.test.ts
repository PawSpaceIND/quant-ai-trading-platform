import {after, test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const directory = fs.mkdtempSync(path.join(os.tmpdir(), "research-dashboard-test-"));
const file = path.join(directory, "dashboard.json");
const variable = "PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT";
after(() => {
  delete process.env[variable];
  fs.rmSync(directory, {recursive: true, force: true});
});

function snapshot() {
  return {
    schemaVersion: 1,
    paperOnly: true,
    generatedAt: new Date().toISOString(),
    modules: ["comparison", "simulation", "companyEvents"].map(id => ({
      id,
      title: id,
      status: "incomplete",
      observedAt: null,
      reason: "Synthetic evidence is incomplete.",
      rows: [{name: "Synthetic source", metrics: [{label: "Status", value: "Unavailable"}]}],
    })),
  };
}

test("dashboard export is explicitly unavailable until configured", async () => {
  delete process.env[variable];
  const {readResearchDashboard} = await import("../lib/research-dashboard");
  assert.deepEqual(readResearchDashboard(), {
    status: "unavailable",
    detail: "No sanitized research dashboard export has been published to this workspace.",
    snapshot: null,
  });
});

test("configured dashboard export is schema-validated and bounded", async () => {
  const {readResearchDashboard} = await import("../lib/research-dashboard");
  process.env[variable] = file;
  assert.equal(readResearchDashboard().status, "invalid");
  fs.writeFileSync(file, JSON.stringify(snapshot()));
  const published = readResearchDashboard();
  assert.equal(published.status, "published");
  assert.equal(published.snapshot?.paperOnly, true);
  fs.writeFileSync(file, JSON.stringify({...snapshot(), paperOnly: false}));
  assert.equal(readResearchDashboard().status, "invalid");
  fs.writeFileSync(file, "x".repeat(512_001));
  assert.equal(readResearchDashboard().status, "invalid");
});
