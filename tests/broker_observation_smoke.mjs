import assert from "node:assert/strict";
import fs from "node:fs";
import {createHash} from "node:crypto";
const origin=process.env.SMOKE_ORIGIN||"http://localhost:3000";
assert(["127.0.0.1","localhost"].includes(new URL(origin).hostname));
const file=process.env.PRAMANA_BROKER_OBSERVATION;assert(file);
const original=fs.readFileSync(file,"utf8");
const canonical=v=>Array.isArray(v)?`[${v.map(canonical).join(",")}]`:v&&typeof v==="object"?`{${Object.keys(v).sort().map(k=>`${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`:JSON.stringify(v);
const write=c=>{const {sha256,...unsigned}=c;fs.writeFileSync(file,JSON.stringify({...unsigned,sha256:createHash("sha256").update(canonical(unsigned)).digest("hex")}));};
assert.equal((await fetch(origin+"/api/broker/observation")).status,401);
const login=await fetch(origin+"/api/session",{method:"POST",headers:{origin,"Content-Type":"application/json"},body:JSON.stringify({secret:process.env.PRAMANA_DASHBOARD_SECRET})});
assert.equal(login.status,200);const cookie=login.headers.get("set-cookie")?.split(";")[0];assert(cookie);
const request=(route,body)=>fetch(origin+route,{method:body?"POST":"GET",headers:{cookie,origin,"Content-Type":"application/json"},...(body?{body:JSON.stringify(body)}:{})});
try {
  const workspace=await(await request("/api/workspace")).json();assert.equal(workspace.liveEnabled,false);assert.equal(workspace.checks.find(c=>c.id==="evidence").pass,false);
  const s=workspace.brokerObservation;assert.equal(s.status,"available",s.detail);assert.deepEqual(s.inspection,{status:"consistent",issueCount:0,issues:[],orderCount:14,tradeCount:26,openOrderCount:1});
  const exported=await request("/api/broker/observation");assert.equal(exported.status,200);assert.equal(exported.headers.get("Cache-Control"),"no-store");assert.match(exported.headers.get("Content-Disposition"),/attachment/);
  assert.deepEqual((await exported.json()).report,s.report);
  const answer=await(await request("/api/copilot",{prompt:"Explain the broker order and trade observation status and remaining pilot gaps."})).json();assert.equal(answer.status,"error");assert.match(answer.error,/Claude is not configured/);
  const context=JSON.parse(answer.context).brokerObservation;assert.equal(context.inspection.orderCount,14);assert.equal(context.accountRef,undefined);assert.equal(context.report,undefined);assert(!JSON.stringify(context).includes("synthetic-1"));
  let c=JSON.parse(original);c.orders[0].filled=1;write(c);
  assert.equal((await(await request("/api/broker/observation")).json()).inspection.status,"changing");
  c.ordersBefore=structuredClone(c.orders);write(c);
  const mismatch=await(await request("/api/broker/observation")).json();assert.equal(mismatch.inspection.status,"issues");assert(mismatch.inspection.issues.some(i=>i.code==="filled_quantity_mismatch"));
  c=JSON.parse(original);c.startedAt="2026-01-01T06:30:00.000+00:00";c.finishedAt=c.startedAt;write(c);assert.equal((await(await request("/api/broker/observation")).json()).status,"stale");
  c.accountRef="0".repeat(64);write(c);assert.equal((await request("/api/broker/observation")).status,503);
  console.log(JSON.stringify({phase:"broker-observation",status:"pass",orders:14,executions:26,authenticated:true,privateExport:true,savedAtlasSummary:true,changingCapture:true,mismatchDetection:true,staleObservation:true,wrongAccountRejected:true,modelCalls:0,brokerCalls:0,liveEnabled:false,operatorAcceptance:false}));
} finally {fs.writeFileSync(file,original);}
