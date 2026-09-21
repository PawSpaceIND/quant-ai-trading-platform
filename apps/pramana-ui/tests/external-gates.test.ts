import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {externalGateChecks} from "../lib/external-gates";

const folder=fs.mkdtempSync(path.join(os.tmpdir(),"external-gates-ui-"));
after(()=>fs.rmSync(folder,{recursive:true,force:true}));

test("external gate UI stays pending without a reviewed report",()=>{
  delete process.env.PRAMANA_EXTERNAL_GATE_REPORT;
  assert.equal(externalGateChecks().every((gate)=>!gate.pass),true);
});

test("external gate UI accepts only a release-matched reviewed report",()=>{
  const file=path.join(folder,"report.json");
  process.env.PRAMANA_RELEASE_REVISION="a".repeat(40);
  process.env.PRAMANA_EXTERNAL_GATE_REPORT=file;
  fs.writeFileSync(file,JSON.stringify({schema:"pramana.external_gate_report.v2",ready:true,liveExecutionEnabled:false,revision:"a".repeat(40),targetHost:"host",gates:[{id:"X01",title:"Real-feed session observation",passed:true,evidenceSha256:"b".repeat(64),detail:"passed"},{id:"X02",title:"Sustained operational burn-in",passed:true,evidenceSha256:"b".repeat(64),detail:"passed"},{id:"X03",title:"Strategy effectiveness",passed:true,evidenceSha256:"b".repeat(64),detail:"passed"}]}));
  assert(externalGateChecks().every((gate)=>gate.pass));
  const current=fs.readFileSync(file,"utf8");
  fs.writeFileSync(file,current.replace("external_gate_report.v2","external_gate_report.v1"));
  assert(externalGateChecks().every((gate)=>!gate.pass && gate.detail !== "passed"));
  fs.writeFileSync(file,current);
  fs.writeFileSync(file,JSON.stringify({schema:"pramana.external_gate_report.v2",ready:true,liveExecutionEnabled:false,revision:"b".repeat(40),gates:[]}));
  assert(externalGateChecks().every((gate)=>!gate.pass));
  fs.writeFileSync(file,JSON.stringify({schema:"pramana.external_gate_report.v2",ready:true,liveExecutionEnabled:false,revision:"a".repeat(41),targetHost:"host",gates:[]}));
  assert(externalGateChecks().every((gate)=>!gate.pass));
  const valid=JSON.parse(fs.readFileSync(file,"utf8"));
  valid.revision="a".repeat(40);
  valid.gates=[{id:"X01",title:"Wrong title",passed:true,evidenceSha256:"b".repeat(64)},{id:"X02",title:"Sustained operational burn-in",passed:true,evidenceSha256:"b".repeat(64)},{id:"X03",title:"Strategy effectiveness",passed:true,evidenceSha256:"b".repeat(64)}];
  fs.writeFileSync(file,JSON.stringify(valid));
  assert(externalGateChecks().every((gate)=>!gate.pass));
  valid.gates.push(valid.gates[2]);
  fs.writeFileSync(file,JSON.stringify(valid));
  assert(externalGateChecks().every((gate)=>!gate.pass));
  delete process.env.PRAMANA_EXTERNAL_GATE_REPORT;
  delete process.env.PRAMANA_RELEASE_REVISION;
});

test("the market payload carries no host path and no engine-only close history", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "market-leak-"));
  const snapshot = path.join(directory, "market.json");
  const previous = process.env.PRAMANA_MARKET_SNAPSHOT;
  process.env.PRAMANA_MARKET_SNAPSHOT = snapshot;
  try {
    // A real collector file, so the parser accepts it and the assertions are not vacuous.
    const valid = JSON.parse(fs.readFileSync(
      new URL("./fixtures/browser-market.json", import.meta.url).pathname, "utf8"));
    fs.writeFileSync(snapshot, JSON.stringify({
      ...valid,
      riskHistory: {"NSE:INFY": [{date: "2026-09-18", close: 1500}]},
      // status + total are what the parser requires before it builds the universe at all.
      instrumentUniverse: {status: "available", total: 1, source: "synthetic",
        recordsPath: "/home/operator/private/instrument-universe.json"},
    }));
    const {readMarket} = await import("../lib/market");
    const market = await readMarket();
    // An absolute path names the engine host's directory layout and its user. Nothing
    // renders it, and /api/market is on the hosted worker's public allow-list.
    // The universe must actually parse, or the next assertion proves nothing.
    assert.equal(market.instrumentUniverse?.status, "available");
    assert.equal((market.instrumentUniverse as Record<string, unknown>).recordsPath, undefined);
    assert.equal(JSON.stringify(market).includes("/home/operator/private"), false);
    // The route withholds the engine-only daily closes the other two consumers strip.
    const {GET} = await import("../app/api/market/route");
    const body = await (await GET()).json();
    assert.equal("riskHistory" in body, false);
    // The rows themselves still arrive, so the strip is targeted and not a blanket empty.
    assert(body.rows.length > 0);
  } finally {
    if (previous === undefined) delete process.env.PRAMANA_MARKET_SNAPSHOT;
    else process.env.PRAMANA_MARKET_SNAPSHOT = previous;
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
