import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {createHash} from "node:crypto";
import type {RunComparisonReport} from "../lib/run-comparison-model";

const folder=fs.mkdtempSync(path.join(os.tmpdir(),"run-comparison-")),file=path.join(folder,"report.json");
process.env.PRAMANA_TENANT_ID="default";
process.env.PRAMANA_LEDGER_PATH=path.join(folder,"missing-ledger.sqlite");
process.env.PRAMANA_CONSOLE_DB=path.join(folder,"console.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT=path.join(folder,"missing-market.json");
process.env.PRAMANA_RUN_COMPARISON=file;
const raw=(name="complete")=>fs.readFileSync(new URL(`../../../tests/fixtures/run-comparison-${name}.json`,import.meta.url),"utf8");
const value=(name="complete"):RunComparisonReport=>JSON.parse(JSON.parse(raw(name)).payload);
const envelope=(report:RunComparisonReport)=>{const payload=JSON.stringify(report);return JSON.stringify({payload,sha256:createHash("sha256").update(payload).digest("hex")});};
const save=(name="complete")=>fs.writeFileSync(file,raw(name));
after(()=>fs.rmSync(folder,{recursive:true,force:true}));

test("window selection discloses retained history and cannot rehash inconsistent coverage",async()=>{
 const {parseRunComparison,runComparisonContext}=await import("../lib/run-comparison");
 const r=value("gapped");r.sources.paper.observationSelection={start:r.window.start,end:r.window.end,calendarSha256:r.window.calendarSha256,expectedMinutes:70,selectedObservations:69,totalAccountObservations:43269,excludedObservations:43200};
 assert.equal(parseRunComparison(envelope(r),"default").report.sources.paper.observationSelection!.totalAccountObservations,43269);
 fs.writeFileSync(file,envelope(r));assert.equal(runComparisonContext().summary!.observationSelection!.excludedObservations,43200);
 for(const change of [{expectedMinutes:69},{selectedObservations:70},{totalAccountObservations:43270},{excludedObservations:-1},{start:"2026-09-11T04:01:00+00:00"},{calendarSha256:"f".repeat(64)}]) {
  const changed=structuredClone(r);Object.assign(changed.sources.paper.observationSelection!,change);assert.throws(()=>parseRunComparison(envelope(changed),"default"));
 }
 const replay=structuredClone(r);replay.sources.replay.observationSelection=replay.sources.paper.observationSelection;assert.throws(()=>parseRunComparison(envelope(replay),"default"));
});

test("Python retained-run fixture agrees with independent browser cash-flow and fill-group arithmetic",async()=>{
 const {parseRunComparison}=await import("../lib/run-comparison");
 const r=parseRunComparison(raw(),"default").report;
 assert.equal(r.curve.length,70);assert.equal(r.initialState,"same_recorded_book");
 const cash:Record<string,number>={};
 for(const mode of ["paper","replay"] as const){cash[mode]=100000;
  for(const f of r.fills[mode])cash[mode]+=(f.side==="SELL"?1:-1)*f.quantity*Number(f.price)-Number(f.cashFees);
  assert(Math.abs(Number(r.curve.at(-1)![mode]!.equity)-cash[mode])<1e-8);
 }
 assert(Math.abs(Number(r.metrics!.endingEquityDifference)-(cash.paper-cash.replay))<1e-8);
 assert.deepEqual(r.fillGroups.map(g=>g.quantityDifference),[-2,-2]);
 assert.equal(r.configuration.strategyEquivalence,"unverified");assert.equal(r.automaticPromotion,false);
});

test("missing, invalid and clock-skewed observations leave three explicit gaps and no whole-window return",async()=>{
 const {parseRunComparison}=await import("../lib/run-comparison");const r=parseRunComparison(raw("gapped"),"default").report;
 assert.equal(r.metrics,null);assert.equal(r.status,"incomplete_observations");
 assert.deepEqual(r.curve.flatMap((p,i)=>p.issues.length?[i]:[]),[20,30,40]);
 assert.equal(r.curve[20].paper!.equity,null);assert.equal(r.curve[30].paper,null);assert.equal(r.curve[40].skewSeconds,15);
});

test("a recomputed envelope cannot hide missing coverage, altered arithmetic, false qualification or invalid dates",async()=>{
 const {parseRunComparison}=await import("../lib/run-comparison");
 const changes:Array<(r:RunComparisonReport)=>void>=[
  r=>r.curve.splice(20,1), r=>r.metrics!.paperReturn="0.9",
  r=>r.curve[10].paper!.cash="0", r=>r.curve[10].paper!.equity="999999",
  r=>r.fillGroups[0].quantityDifference=100, r=>r.fillGroups[0].paper!.cashFees="900",
  r=>r.fills.paper[0].quantity=9, r=>r.window.excludedClosedMinutes=100,
  r=>r.window.start="2026-02-30T04:00:00+00:00",r=>r.configuration.observedUnqualifiedPoints=0,
  r=>r.automaticPromotion=true as false,r=>r.initialState="different_recorded_book",
 ];
 for(const change of changes){const r=value();change(r);assert.throws(()=>parseRunComparison(envelope(r),"default"),String(change));}
 assert.throws(()=>parseRunComparison(raw(),"other"));
 const g=value("gapped");g.curve[40].issues=[];assert.throws(()=>parseRunComparison(envelope(g),"default"));
 assert.throws(()=>parseRunComparison(raw().replace('"sha256":','"extra":true,"sha256":'),"default"));
});

test("private workspace/export bind the selected hash and fail closed on report replacement or corruption",async()=>{
 save();const {GET}=await import("../app/api/research/run-comparison/route");
 const request=(query="")=>GET(new Request("http://localhost/api/research/run-comparison"+query));
 const {sha256}=JSON.parse(raw());
 for(const q of ["?sha256=x",`?sha256=${sha256}&sha256=${sha256}`])assert.equal((await request(q)).status,400);
 const response=await request(`?sha256=${sha256}`);assert.equal(response.status,200);assert.equal(response.headers.get("cache-control"),"no-store");assert.match(response.headers.get("content-disposition")!,/attachment/);
 const exported=await response.json();assert.equal(exported.sha256,sha256);
 const {GET:workspace}=await import("../app/api/workspace/route");const w=await(await workspace()).json();assert.deepEqual(w.runComparison,exported);
 save("gapped");assert.equal((await request(`?sha256=${sha256}`)).status,503);
 fs.writeFileSync(file,'{"payload":"changed","sha256":"'+sha256+'"}');assert.equal((await request()).status,503);
 fs.rmSync(file);assert.equal((await request()).status,503);
 delete process.env.PRAMANA_RUN_COMPARISON;assert.equal((await request()).status,422);process.env.PRAMANA_RUN_COMPARISON=file;
});

test("Atlas receives only the selected historical comparison and excludes earlier chat/current account/raw identifiers",async()=>{
 save("gapped");delete process.env.ANTHROPIC_API_KEY;
 const {generateAnswer,conversations}=await import("../lib/copilot");const {sha256}=JSON.parse(raw("gapped"));
 const prior=await generateAnswer("Current account secret prior prompt",undefined,"run-prior");
 const selected=await generateAnswer("Review selected report",prior.id,"run-selected",fetch,undefined,undefined,sha256);
 assert.equal(selected.status,"error");const saved=JSON.parse(conversations(selected.id)[0].context!);
 assert.equal(saved.mode,"run_comparison_review");assert.equal(saved.runComparison.reportSha256,sha256);assert.equal(saved.runComparison.summary.pairedMinutes,67);
 assert.equal(saved.runComparison.summary.metrics,null);assert.equal(saved.runComparison.summary.configuration.strategyEquivalence,"unverified");
 for(const key of ["portfolio","market","swarm","benchmarkPerformance","riskControls"])assert.equal(saved[key],undefined);
 const context=JSON.stringify(saved);assert(!context.includes("Current account secret"));assert(!context.includes("orderId"));assert(!context.includes("sourceSha256"));
 save();await assert.rejects(()=>generateAnswer("Changed",undefined,"run-replaced",fetch,undefined,undefined,sha256),/changed/);
 await assert.rejects(()=>generateAnswer("Ambiguous",undefined,"run-ambiguous",fetch,"2026-09-11T04:00:00Z",undefined,sha256),/Select one/);
});


test("microsecond observation differences remain exact and cannot round into the allowed tolerance",async()=>{
 const {parseRunComparison}=await import("../lib/run-comparison");
 const r=value("gapped");r.curve[40].paper!.at="2026-09-11T04:40:15.000001+00:00";r.curve[40].skewSeconds=15.000001;
 r.window.maxSkewSeconds=15;
 assert.equal(parseRunComparison(envelope(r),"default").report.curve[40].skewSeconds,15.000001);
 r.curve[40].issues=[];assert.throws(()=>parseRunComparison(envelope(r),"default"));
});
