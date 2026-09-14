import assert from "node:assert/strict";
import fs from "node:fs";
const origin=process.env.SMOKE_ORIGIN||"http://localhost:3000";
assert(["localhost","127.0.0.1"].includes(new URL(origin).hostname));
assert.equal((await fetch(origin+"/api/research/run-comparison")).status,401);
const login=await fetch(origin+"/api/session",{method:"POST",headers:{origin,"Content-Type":"application/json"},body:JSON.stringify({secret:process.env.PRAMANA_DASHBOARD_SECRET})});
assert.equal(login.status,200);const cookie=login.headers.get("set-cookie")?.split(";")[0];assert(cookie);
const request=(route,body)=>fetch(origin+route,{method:body?"POST":"GET",headers:{cookie,origin,"Content-Type":"application/json"},...(body?{body:JSON.stringify(body)}:{})});
const workspace=await(await request("/api/workspace")).json(),state=workspace.runComparison;
assert.equal(workspace.liveEnabled,false);assert.equal(workspace.checks.find(c=>c.id==="evidence").pass,false);
assert.equal(state.status,"published",state.detail);assert.equal(state.report.curve.length,70);assert.equal(state.report.metrics,null);
assert.deepEqual(state.report.curve.flatMap((p,i)=>p.issues.length?[i]:[]),[20,30,40]);
assert.equal(state.report.configuration.strategyEquivalence,"unverified");assert.equal(state.report.automaticPromotion,false);
assert.deepEqual(state.report.fillGroups.map(g=>g.quantityDifference),[-2,-2]);
const selected=await request(`/api/research/run-comparison?sha256=${state.sha256}`);assert.equal(selected.status,200);
assert.equal(selected.headers.get("cache-control"),"no-store");assert.match(selected.headers.get("content-disposition"),/attachment/);assert.deepEqual(await selected.json(),state);
assert.equal((await request(`/api/research/run-comparison?sha256=${"0".repeat(64)}`)).status,503);
const answer=await(await request("/api/copilot",{prompt:"Review the selected paper versus replay comparison and its gaps.",runComparisonSha256:state.sha256})).json();
assert.equal(answer.status,"error");assert.match(answer.error,/Claude is not configured/);
const context=JSON.parse(answer.context);assert.equal(context.mode,"run_comparison_review");assert.equal(context.runComparison.reportSha256,state.sha256);assert.equal(context.runComparison.summary.pairedMinutes,67);assert.equal(context.runComparison.summary.metrics,null);
assert.equal(context.portfolio,undefined);assert.equal(context.market,undefined);assert(!JSON.stringify(context).includes("orderId"));
// Verify the running production server notices report corruption, then restore exactly its input bytes.
const file=process.env.PRAMANA_RUN_COMPARISON,original=fs.readFileSync(file);
try {fs.writeFileSync(file,'{"invalid":true}');const bad=await(await request("/api/research/run-comparison")).json();assert.equal(bad.status,"invalid");assert.equal(bad.report,null);}
finally {fs.writeFileSync(file,original);}
assert.equal((await request(`/api/research/run-comparison?sha256=${state.sha256}`)).status,200);
console.log(JSON.stringify({phase:process.env.RUN_COMPARISON_PHASE||"run-comparison",status:"pass",reportSha256:state.sha256,runId:state.report.configuration.replayRunId,minutes:70,pairedMinutes:67,explicitGaps:3,paperFills:2,replayFills:2,selectedExport:true,selectedAtlasContext:true,corruptionWithholdsResult:true,modelCalls:0,brokerCalls:0,liveEnabled:false,operatorAcceptance:false}));
