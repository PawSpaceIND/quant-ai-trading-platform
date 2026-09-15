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
