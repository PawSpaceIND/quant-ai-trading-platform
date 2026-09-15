import { after, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash, createHmac } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import type { Runtime } from "../lib/pilot";

const dir=fs.mkdtempSync(path.join(os.tmpdir(),"pramana-strategy-test-"));
process.env.PRAMANA_TENANT_ID="default";
process.env.PRAMANA_LEDGER_PATH=path.join(dir,"ledger.db");
process.env.PRAMANA_REVIEW_DIR=dir;
process.env.PRAMANA_CONSOLE_DB=path.join(dir,"console.db");
process.env.PRAMANA_MARKET_SNAPSHOT=path.join(dir,"absent-market.json");
process.env.PRAMANA_PROOF_DIR=path.join(dir,"absent-proofs");
process.env.PRAMANA_REVIEW_SECRET="synthetic-review-key-not-production-123";
process.env.PRAMANA_RELEASE_REVISION="a".repeat(40);
const sourceHash="b".repeat(64),evidenceHash="e".repeat(64);
const payload=JSON.stringify({schema:"pramana.runtime_strategy.v1",tenant_id:"default",release_revision:"a".repeat(40),source:{sha256:sourceHash}});
const sha=createHash("sha256").update(payload).digest("hex");
process.env.PRAMANA_STRATEGY_CONFIG_SHA256=sha;
const db=new DatabaseSync(process.env.PRAMANA_LEDGER_PATH);
db.exec("CREATE TABLE pilot_strategy_manifests(tenant_id TEXT,sha256 TEXT,payload TEXT); CREATE TABLE paper_ledger(id INTEGER,tenant_id TEXT); CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,payload TEXT)");
db.prepare("INSERT INTO pilot_strategy_manifests VALUES('default',?,?)").run(sha,payload);
db.exec("INSERT INTO paper_ledger VALUES(200,'default')");
after(()=>{db.close();fs.rmSync(dir,{recursive:true,force:true});});
const dates:string[]=[];
for(let i=0;dates.length<30;i++) {const d=new Date(Date.UTC(2000,0,3+i));if(d.getUTCDay()!==0 && d.getUTCDay()!==6)dates.push(d.toISOString().slice(0,10));}
function observations() {
  db.exec("DELETE FROM paper_live_valuations; BEGIN");
  const insert=db.prepare("INSERT INTO paper_live_valuations VALUES('default',?,?)");
  for(const date of dates) for(let i=0;i<300;i++) {
    const timestamp=new Date(Date.parse(`${date}T05:00:00Z`)+i*60000).toISOString();
    insert.run(timestamp,JSON.stringify({sessionDate:date,updatedAt:timestamp,qualifyingSession:true,allMarksFresh:true,totalEquity:100000,strategyObservation:{manifestSha256:sha,eligible:true}}));
  }
  db.exec("COMMIT");
}
function fresh():Runtime {
  return {status:"running",mode:"paper",updatedAt:new Date().toISOString(),strategyManifest:{status:"matched",sha256:sha,bootSha256:sha,sourceSha256:sourceHash,releaseRevision:"a".repeat(40),checkedAt:new Date().toISOString(),sourceCheckAgeSeconds:0,issues:[]},
    strategyEvidence:{schema:"pramana.strategy_episode_evidence.v1",status:"ok",strategySha256:sha,sourceSha256:"f".repeat(64),evidenceSha256:evidenceHash,generatedAt:new Date().toISOString(),coverageStartedAt:"2000-01-01T00:00:00Z",ledgerId:200,unresolvedEpisodes:0,foreignOpenEpisodes:0,unlinkedAccountCompletedTrades:0,incompatibleSessionDates:[],summary:{completedTrades:100,openEpisodes:0,netPnl:"100",expectancy:"1",profitFactor:"1.5",profitFactorState:"defined",winRate:".6",closedCashFees:"10"}}};
}
function sign(overrides:Record<string,unknown>={}) {
  const artifact={strategy_config_sha256:sha,strategy_evidence_sha256:evidenceHash,holdout_evidence_sha256:"1".repeat(64),forward_paper_evidence_sha256:"2".repeat(64),execution_stress_evidence_sha256:"3".repeat(64),calibration_evidence_sha256:"4".repeat(64),holdout_reviewed:true,costs_reviewed:true,trial_register_reviewed:true,ai_calibration_reviewed:true,forward_paper_reviewed:true,execution_stress_reviewed:true,sample_trades:100,expectancy:"1",profit_factor:"1.5",paper_days:30,...overrides};
  const body=JSON.stringify({schema:"pramana.pilot.acceptance.v1",scope:"private-paper-pilot",gate:"strategy",tenant_id:"default",release_revision:"a".repeat(40),reviewer:"Synthetic QA",reviewed_at:new Date().toISOString(),expires_at:new Date(Date.now()+86400000).toISOString(),artifact});
  fs.writeFileSync(path.join(dir,"strategy.json"),JSON.stringify({payload:body,signature:createHmac("sha256",process.env.PRAMANA_REVIEW_SECRET!).update(body).digest("hex")}));
}

test("only distinct, same-configuration minute observations count toward days",async()=>{
  const { strategyObservationDays }=await import("../lib/strategy-evidence");
  observations();
  assert.equal(strategyObservationDays(sha,[],"2000-01-01T00:00:00Z").days,30);
  assert.equal(strategyObservationDays(sha,[dates[0]],"2000-01-01T00:00:00Z").days,29);
  const first=db.prepare("SELECT rowid,payload FROM paper_live_valuations ORDER BY timestamp LIMIT 1").get() as {rowid:number;payload:string};
  const original=JSON.parse(first.payload);
  db.prepare("UPDATE paper_live_valuations SET payload=? WHERE rowid=?").run(JSON.stringify({...original,strategyObservation:undefined}),first.rowid);
  assert.equal(strategyObservationDays(sha,[],"2000-01-01T00:00:00Z").days,29); // Legacy minutes do not qualify this strategy.
  db.prepare("UPDATE paper_live_valuations SET payload=? WHERE rowid=?").run(JSON.stringify({...original,strategyObservation:{manifestSha256:"c".repeat(64),eligible:true}}),first.rowid);
  assert.equal(strategyObservationDays(sha,[],"2000-01-01T00:00:00Z").days,29);
  observations();
  db.prepare("DELETE FROM paper_live_valuations WHERE timestamp NOT LIKE ?").run(`${dates[0]}%`);
  db.prepare("DELETE FROM paper_live_valuations WHERE timestamp=?").run(`${dates[0]}T05:00:00.000Z`);
  const duplicate=db.prepare("SELECT timestamp,payload FROM paper_live_valuations LIMIT 1").get() as {timestamp:string;payload:string};
  db.prepare("INSERT INTO paper_live_valuations VALUES('default',?,?)").run(duplicate.timestamp,duplicate.payload);
  assert.equal(strategyObservationDays(sha,[],"2000-01-01T00:00:00Z").days,0); // 300 rows, only 299 distinct minutes.
  observations();
});

test("strategy evidence cannot survive a new fill or hide unresolved and foreign episodes",async()=>{
  const { currentStrategyEvidence }=await import("../lib/strategy-evidence");
  assert(currentStrategyEvidence(fresh(),sha));
  for(const property of ["unresolvedEpisodes","foreignOpenEpisodes"] as const) {
    const r=fresh();r.strategyEvidence![property]=1;assert(!currentStrategyEvidence(r,sha));
  }
  const other=fresh();other.strategyEvidence!.strategySha256="d".repeat(64);assert(!currentStrategyEvidence(other,sha));
  db.exec("INSERT INTO paper_ledger VALUES(201,'default')");assert(!currentStrategyEvidence(fresh(),sha));
  db.exec("DELETE FROM paper_ledger WHERE id=201");
});

test("signed review must match strategy-linked counts, evidence hash, metrics and days",async()=>{
  const { reviewedGate }=await import("../lib/review");
  sign();assert(reviewedGate("strategy",fresh()).pass);
  for(const change of [{strategy_evidence_sha256:"d".repeat(64)},{sample_trades:101},{expectancy:"2"},{profit_factor:"1.6"},{paper_days:31}]) {
    sign(change);assert(!reviewedGate("strategy",fresh()).pass,JSON.stringify(change));
  }
  sign();const unproven=fresh();unproven.strategyEvidence!.unresolvedEpisodes=1;assert(!reviewedGate("strategy",unproven).pass);
  const wrong=fresh();wrong.strategyEvidence!.summary.completedTrades=99;assert(!reviewedGate("strategy",wrong).pass);
  db.exec("UPDATE paper_live_valuations SET payload=json_remove(payload,'$.strategyObservation')");
  assert(!reviewedGate("strategy",fresh()).pass); // Account history is insufficient without configuration linkage.
});


test("Atlas persists the same strategy evidence and qualified days supplied to the model",async()=>{
  observations();
  db.exec("ALTER TABLE paper_live_valuations ADD COLUMN ledger_id INTEGER DEFAULT 200; CREATE TABLE pilot_runtime(tenant_id TEXT,payload TEXT)");
  db.prepare("INSERT INTO pilot_runtime VALUES('default',?)").run(JSON.stringify(fresh()));
  const { generateAnswer, conversations }=await import("../lib/copilot");
  process.env.ANTHROPIC_API_KEY="synthetic-no-network-key";
  let system="";
  const transport:typeof fetch=async(_url,init)=>{
    system=JSON.parse(String(init?.body)).system;
    return Response.json({content:[{type:"text",text:"Synthetic context verification only"}],usage:{input_tokens:10,output_tokens:5}});
  };
  const result=await generateAnswer("Explain my strategy evidence",undefined,"synthetic-attribution-context",transport);
  assert.equal(result.status,"complete");
  const context=JSON.parse(conversations(result.id)[0].context!);
  assert.equal(context.runtime.strategyEvidence.evidenceSha256,evidenceHash);
  assert.equal(context.runtime.strategyEvidence.summary.completedTrades,100);
  assert.equal(context.strategyObservation.days,30);
  assert(system.includes(evidenceHash));
  assert(system.includes('"strategyObservation":{"days":30'));
  delete process.env.ANTHROPIC_API_KEY;
});
