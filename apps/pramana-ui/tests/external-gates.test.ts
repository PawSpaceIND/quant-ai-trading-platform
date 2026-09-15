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
  fs.writeFileSync(file,JSON.stringify({schema:"pramana.external_gate_report.v1",ready:true,liveExecutionEnabled:false,revision:"a".repeat(40),targetHost:"host",gates:[{id:"X01",passed:true,evidenceSha256:"b".repeat(64),detail:"passed"},{id:"X02",passed:true,evidenceSha256:"b".repeat(64),detail:"passed"},{id:"X03",passed:true,evidenceSha256:"b".repeat(64),detail:"passed"}]}));
  assert(externalGateChecks().every((gate)=>gate.pass));
  fs.writeFileSync(file,JSON.stringify({schema:"pramana.external_gate_report.v1",ready:true,liveExecutionEnabled:false,revision:"b".repeat(40),gates:[]}));
  assert(externalGateChecks().every((gate)=>!gate.pass));
  fs.writeFileSync(file,JSON.stringify({schema:"pramana.external_gate_report.v1",ready:true,liveExecutionEnabled:false,revision:"a".repeat(41),targetHost:"host",gates:[]}));
  assert(externalGateChecks().every((gate)=>!gate.pass));
  delete process.env.PRAMANA_EXTERNAL_GATE_REPORT;
  delete process.env.PRAMANA_RELEASE_REVISION;
});
