import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import {createHash} from "node:crypto";
import type {BrokerCapture} from "../lib/broker-observation";

const folder=fs.mkdtempSync(path.join(os.tmpdir(),"broker-observation-")),file=path.join(folder,"capture.json");
process.env.PRAMANA_TENANT_ID="default";process.env.PRAMANA_LEDGER_PATH=path.join(folder,"paper.sqlite");process.env.PRAMANA_CONSOLE_DB=path.join(folder,"console.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT=path.join(folder,"market.json");process.env.PRAMANA_PROOF_DIR=path.join(folder,"proofs");process.env.ANTHROPIC_API_KEY="";
const fixture=JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/broker-observation.json",import.meta.url),"utf8")) as BrokerCapture;
const canonical=(v:any):string=>Array.isArray(v)?`[${v.map(canonical).join(",")}]`:v&&typeof v==="object"?`{${Object.keys(v).sort().map(k=>`${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`:JSON.stringify(v);
function signed(c:BrokerCapture) {const {sha256,...unsigned}=c;return {...unsigned,sha256:createHash("sha256").update(canonical(unsigned)).digest("hex")};}
function write(c:BrokerCapture=fixture) {process.env.PRAMANA_BROKER_OBSERVATION=file;process.env.PRAMANA_BROKER_ACCOUNT_REF=c.accountRef;fs.writeFileSync(file,JSON.stringify(c));}
after(()=>fs.rmSync(folder,{recursive:true,force:true}));

test("independent reader matches Python split-fill, partial and cancellation evidence",async()=>{
  const {parseBrokerCapture,inspectBrokerCapture,readBrokerObservation}=await import("../lib/broker-observation");
  const c=parseBrokerCapture(JSON.stringify(fixture),"default",fixture.accountRef);
  assert.deepEqual(inspectBrokerCapture(c),{status:"consistent",issueCount:0,issues:[],orderCount:14,tradeCount:26,openOrderCount:1});
  write();assert.equal(readBrokerObservation(Date.parse(c.finishedAt)).status,"available");assert.equal(readBrokerObservation(Date.parse(c.finishedAt)+120001).status,"stale");
  assert.equal(readBrokerObservation(Date.parse(c.finishedAt)-6000).status,"invalid");
});

test("malformed identity, time, price and tampered evidence cannot produce a result",async()=>{
  const {parseBrokerCapture}=await import("../lib/broker-observation");
  for(const change of [
    (c:BrokerCapture)=>c.tenantId="other",(c:BrokerCapture)=>c.accountRef="0".repeat(64),
    (c:BrokerCapture)=>c.orders[0].quantity=1.5,(c:BrokerCapture)=>c.orders[0].filled=-1,
    (c:BrokerCapture)=>c.trades[0].price="NaN",(c:BrokerCapture)=>c.trades[0].price="0.00000000",
    (c:BrokerCapture)=>c.orders.push(c.orders[0]),(c:BrokerCapture)=>c.trades.push(c.trades[0]),
    (c:BrokerCapture)=>c.finishedAt="2026-09-11T06:30:31+00:00",(c:BrokerCapture)=>c.finishedAt="2026-09-11T06:30:00",
    (c:BrokerCapture)=>c.orders[0].at="2026-02-31T06:30:00+00:00",(c:BrokerCapture)=>(c as any).unexpected="private",
  ]) {const c=structuredClone(fixture);change(c);assert.throws(()=>parseBrokerCapture(JSON.stringify(signed(c)),"default",fixture.accountRef),String(change));}
  const c=structuredClone(fixture);c.orders[0].symbol="CHANGED";
  assert.throws(()=>parseBrokerCapture(JSON.stringify(c),"default",fixture.accountRef),/hash/);
});

test("mismatch and changing captures are distinct; empty evidence never qualifies",async()=>{
  const {inspectBrokerCapture}=await import("../lib/broker-observation");
  for(const [change,code] of [
    [(c:BrokerCapture)=>c.orders[0].filled=1,"filled_quantity_mismatch"],
    [(c:BrokerCapture)=>c.orders[0].averagePrice="100.02000000","complete_average_price_mismatch"],
    [(c:BrokerCapture)=>c.trades[0].side="SELL","trade_identity_mismatch"],
    [(c:BrokerCapture)=>c.trades[0].orderId="absent","orphan_trade"],
    [(c:BrokerCapture)=>c.orders[0].variety="iceberg","unsupported_order_variety"],
    [(c:BrokerCapture)=>c.orders[0].status="UNKNOWN","unsupported_status"],
  ] as const) {const c=structuredClone(fixture);change(c);assert.equal(inspectBrokerCapture(c).status,"changing");c.ordersBefore=structuredClone(c.orders);c.tradesBefore=structuredClone(c.trades);const s=inspectBrokerCapture(c);assert.equal(s.status,"issues");assert(s.issues.some(i=>i.code===code));}
  const empty={...fixture,orders:[],ordersBefore:[],trades:[],tradesBefore:[]};assert.equal(inspectBrokerCapture(empty).status,"empty");
  const tolerant=structuredClone(fixture);tolerant.orders[0].averagePrice="100.01000000";tolerant.ordersBefore=structuredClone(tolerant.orders);assert.equal(inspectBrokerCapture(tolerant).status,"consistent");
});

test("private export and saved Atlas context retain status while excluding external account details",async t=>{
  write();t.mock.method(Date,"now",()=>Date.parse(fixture.finishedAt));
  const {GET}=await import("../app/api/broker/observation/route"), request=new Request("http://localhost/api/broker/observation"), response=GET(request);
  assert.equal(response.status,200);assert.equal(response.headers.get("Cache-Control"),"no-store");assert.match(response.headers.get("Content-Disposition")!,/attachment/);
  const exported=await response.json();assert.deepEqual(exported.report,fixture);assert.equal(exported.inspection.status,"consistent");
  const {generateAnswer}=await import("../lib/copilot");
  const answer=await generateAnswer("Explain the external broker consistency checks.",undefined,"broker-test",async()=>{throw new Error("Unexpected model call");});
  assert.equal(answer.status,"error");const context=JSON.parse(answer.context!);
  assert.equal(context.brokerObservation.inspection.orderCount,14);assert.equal(context.brokerObservation.status,"available");
  for(const sentinel of [fixture.accountRef,"synthetic-1","738561","trade-1-a","ordersBefore","averagePrice"])
    assert(!JSON.stringify(context.brokerObservation).includes(sentinel),sentinel);
  process.env.PRAMANA_BROKER_ACCOUNT_REF="f".repeat(64);assert.equal(GET(request).status,503);
  delete process.env.PRAMANA_BROKER_OBSERVATION;assert.equal(GET(request).status,422);
});
