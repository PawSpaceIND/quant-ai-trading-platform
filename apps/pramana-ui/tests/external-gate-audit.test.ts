import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {externalGateChecks} from "../lib/external-gates";

test("audit A01: external acceptance requires explicit matching deployment identity", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "gate-audit-"));
  const previousFile = process.env.PRAMANA_EXTERNAL_GATE_REPORT;
  const previousRevision = process.env.PRAMANA_RELEASE_REVISION;
  try {
    const file = path.join(directory, "report.json");
    process.env.PRAMANA_EXTERNAL_GATE_REPORT = file;
    fs.writeFileSync(file, JSON.stringify({
      schema: "pramana.external_gate_report.v2", ready: true, liveExecutionEnabled: false,
      revision: "a".repeat(40), targetHost: "fixture",
      gates: [["X01", "Real-feed session observation"], ["X02", "Sustained operational burn-in"],
        ["X03", "Strategy effectiveness"]].map(([id, title]) => ({
          id, title, passed: true, evidenceSha256: "b".repeat(64)
        }))
    }));
    for (const revision of [undefined, "", "   ", "not-a-sha", "a".repeat(41), "b".repeat(40)]) {
      if (revision === undefined) delete process.env.PRAMANA_RELEASE_REVISION;
      else process.env.PRAMANA_RELEASE_REVISION = revision;
      assert(externalGateChecks().every(gate => !gate.pass), String(revision));
    }
    process.env.PRAMANA_RELEASE_REVISION = "a".repeat(40);
    assert(externalGateChecks().every(gate => gate.pass));
  } finally {
    if (previousFile === undefined) delete process.env.PRAMANA_EXTERNAL_GATE_REPORT;
    else process.env.PRAMANA_EXTERNAL_GATE_REPORT = previousFile;
    if (previousRevision === undefined) delete process.env.PRAMANA_RELEASE_REVISION;
    else process.env.PRAMANA_RELEASE_REVISION = previousRevision;
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
