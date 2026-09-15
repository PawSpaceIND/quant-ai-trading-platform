import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {createHash} from "node:crypto";
import {DatabaseSync} from "node:sqlite";
import {canonical,type BrokerCapture} from "../lib/broker-capture";
const folder=fs.mkdtempSync(path.join(os.tmpdir(),"broker-journal-")),file=path.join(folder,"broker.sqlite");
process.env.PRAMANA_TENANT_ID="default";process.env.PRAMANA_LEDGER_PATH=path.join(folder,"paper.sqlite");process.env.PRAMANA_CONSOLE_DB=path.join(folder,"console.sqlite");process.env.PRAMANA_MARKET_SNAPSHOT=path.join(folder,"market.json");process.env.PRAMANA_PROOF_DIR=path.join(folder,"proofs");
const fixture=JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/broker-journal.json",import.meta.url),"utf8"));
const journalId="a".repeat(32),now=Date.parse(fixture.captures.at(-1).finishedAt);
const hash=(v:unknown)=>createHash("sha256").update(canonical(v)).digest("hex");
function source(){
  delete process.env.PRAMANA_BROKER_OBSERVATION;process.env.PRAMANA_BROKER_JOURNAL=file;process.env.PRAMANA_BROKER_ACCOUNT_REF=fixture.captures[0].accountRef;
  fs.rmSync(file,{force:true});const db=new DatabaseSync(file);db.exec("PRAGMA journal_mode=WAL; CREATE TABLE broker_journal_meta(id INTEGER PRIMARY KEY,version INTEGER,journal_id TEXT,tenant TEXT,account_ref TEXT); CREATE TABLE broker_captures(sequence INTEGER PRIMARY KEY,previous_hash TEXT,entry_hash TEXT,capture_sha TEXT,payload TEXT)");
  db.prepare("INSERT INTO broker_journal_meta VALUES (1,1,?,'default',?)").run(journalId,fixture.captures[0].accountRef);
  let previousHash:string|null=null;
  fixture.captures.forEach((c:BrokerCapture,i:number)=>{const h=hash({journalId,sequence:i+1,previousHash,captureSha256:c.sha256});db.prepare("INSERT INTO broker_captures VALUES (?,?,?,?,?)").run(i+1,previousHash,h,c.sha256,JSON.stringify(c));previousHash=h;});db.close();
}
function mutate(sql:string){const db=new DatabaseSync(file);try{db.exec(sql);}finally{db.close();}}
after(()=>fs.rmSync(folder,{recursive:true,force:true}));

test("Node reconstructs all Python lifecycle expectations including repeated missing records and recovery",async()=>{
  source();const {readBrokerObservation}=await import("../lib/broker-observation");
  for(const expected of fixture.expected){
    const c=fixture.captures[expected.sequence-1];
    const state=readBrokerObservation(now,{journalId,sequence:expected.sequence,captureSha256:c.sha256});
    assert(state.report,state.detail);assert.deepEqual(state.inspection,expected.inspection);assert.deepEqual(state.journal!.temporal,expected.temporal);assert.equal(state.journal!.cumulativeIssueCount,expected.cumulativeIssueCount);assert.equal(state.journal!.history.length,6);
  }
  const latest=readBrokerObservation(now);assert.equal(latest.inspection!.status,"consistent");assert.equal(latest.journal!.temporal.issueCount,0);assert(latest.journal!.cumulativeIssueCount>0);
  assert.equal(readBrokerObservation(now+120001).status,"stale");
});

test("private API selects immutable captures and rejects invalid or changed references",async t=>{
  source();t.mock.method(Date,"now",()=>now);const {GET}=await import("../app/api/broker/observation/route");
  const selected=await GET(new Request(`http://localhost/api/broker/observation?journalId=${journalId}&sequence=2&captureSha256=${fixture.captures[1].sha256}`)).json();
  assert.equal(selected.journal.selectedSequence,2);assert.equal(selected.journal.temporal.status,"compared");
  assert.equal(GET(new Request(`http://localhost/api/broker/observation?journalId=${journalId}&sequence=2&captureSha256=${"0".repeat(64)}`)).status,503);
  for(const q of ["sequence=-1","sequence=1.5",`journalId=${journalId}&sequence=2&sequence=3`,`journalId=${journalId}&captureSha256=wrong`])assert.equal(GET(new Request(`http://localhost/api/broker/observation?${q}`)).status,400);
  assert.equal(GET(new Request(`http://localhost/api/broker/observation?journalId=${journalId}&sequence=999`)).status,503);
});

test("a historical Atlas request excludes later findings, current workspace and prior conversation",async t=>{
  source();t.mock.method(Date,"now",()=>now);process.env.ANTHROPIC_API_KEY="synthetic-test-key";
  const {generateAnswer}=await import("../lib/copilot");
  const parent=await generateAnswer("FUTURE_PRIVATE_SENTINEL",undefined,"broker-parent",async()=>new Response(JSON.stringify({content:[{type:"text",text:"FUTURE_RESPONSE_SENTINEL"}]})));
  assert.equal(parent.status,"complete");let body:any;
  const result=await generateAnswer("Explain the early captured partial fill",parent.id,"broker-historical",async(_url,options)=>{body=JSON.parse(String(options?.body));return new Response(JSON.stringify({content:[{type:"text",text:"Synthetic response"}]}));},undefined,{journalId,sequence:1,captureSha256:fixture.captures[0].sha256});
  const context=JSON.parse(result.context!);assert.equal(context.mode,"broker_capture_review");assert.equal(context.brokerObservation.lifecycle.sequence,1);assert.equal(context.brokerObservation.lifecycle.cumulativeIssueCount,0);
  assert.equal(context.portfolio,undefined);assert.equal(context.market,undefined);assert.equal(context.brokerObservation.lifecycle.temporal.changes,undefined);
  assert.equal(body.messages.length,1);assert(!JSON.stringify(body).includes("FUTURE_PRIVATE_SENTINEL"));assert(!JSON.stringify(body).includes("FUTURE_RESPONSE_SENTINEL"));
  for(const secret of [fixture.captures[0].accountRef,"synthetic-13","trade-13-b","738561","headHash"])assert(!JSON.stringify(context).includes(secret));
  await assert.rejects(generateAnswer("invalid",undefined,"bad-broker",undefined,undefined,{journalId,sequence:1,captureSha256:"f".repeat(64)}),/unavailable/);
  await assert.rejects(generateAnswer("conflicting",undefined,"bad-mixed",undefined,"2026-09-11T00:00:00Z",{journalId,sequence:1,captureSha256:fixture.captures[0].sha256}),/Select one/);
  process.env.ANTHROPIC_API_KEY="";
});

test("malformed or incomplete history never silently falls back to a current capture",async()=>{
  const {readBrokerObservation}=await import("../lib/broker-observation");
  for(const sql of ["DELETE FROM broker_captures WHERE sequence=3","UPDATE broker_captures SET payload='{}' WHERE sequence=1","UPDATE broker_journal_meta SET tenant='another'","UPDATE broker_captures SET previous_hash='wrong' WHERE sequence=2"]){source();mutate(sql);const state=readBrokerObservation(now);assert.equal(state.status,"invalid");assert.equal(state.report,null);}
  source();process.env.PRAMANA_BROKER_OBSERVATION="also-configured";assert.equal(readBrokerObservation(now).status,"invalid");delete process.env.PRAMANA_BROKER_OBSERVATION;
});

test("concurrent WAL append cannot mix capture count, head and selected evidence across snapshots",async t=>{
  source();const {readBrokerObservation}=await import("../lib/broker-observation");
  const original=DatabaseSync.prototype.prepare;let appended=false;
  t.mock.method(DatabaseSync.prototype,"prepare",function(this:DatabaseSync,sql:string){
    if(!appended&&sql.includes("SELECT count(*) AS count")){
      appended=true;const writer=new DatabaseSync(file);const c=structuredClone(fixture.captures.at(-1));c.startedAt=c.finishedAt="2026-09-11T06:30:01.000+00:00";const {sha256,...unsigned}=c;c.sha256=hash(unsigned);
      const last=original.call(writer,"SELECT entry_hash FROM broker_captures ORDER BY sequence DESC LIMIT 1").get() as {entry_hash:string};
      original.call(writer,"INSERT INTO broker_captures VALUES (?,?,?,?,?)").run(7,last.entry_hash,hash({journalId,sequence:7,previousHash:last.entry_hash,captureSha256:c.sha256}),c.sha256,JSON.stringify(c));writer.close();
    }
    return original.call(this,sql);
  });
  const old=readBrokerObservation(now+1000);assert.equal(old.journal!.captureCount,6);assert.equal(old.journal!.selectedSequence,6);
  const fresh=readBrokerObservation(now+1000);assert.equal(fresh.journal!.captureCount,7);assert.equal(fresh.journal!.selectedSequence,7);assert.notEqual(old.journal!.headHash,fresh.journal!.headHash);
});

test("cancelled pending/average cleanup is not a terminal reversal",async()=>{
  const {BrokerLifecycle}=await import("../lib/broker-lifecycle"),{inspectBrokerCapture}=await import("../lib/broker-capture");
  const replay=new BrokerLifecycle(),first=structuredClone(fixture.captures[0]) as BrokerCapture;
  replay.inspect(first,inspectBrokerCapture(first));const later=structuredClone(first);
  const order=later.orders.find(o=>o.status==="CANCELLED")!;order.pending=0;order.averagePrice="98.00000000";later.ordersBefore=structuredClone(later.orders);
  const observed=inspectBrokerCapture(later);assert.equal(observed.status,"consistent");const temporal=replay.inspect(later,observed);assert.equal(temporal.issueCount,0);assert.equal(temporal.changeCount,2);
});
