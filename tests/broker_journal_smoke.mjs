import assert from "node:assert/strict";
const origin=process.env.SMOKE_ORIGIN||"http://localhost:3000";
assert(["localhost","127.0.0.1"].includes(new URL(origin).hostname));
assert.equal((await fetch(origin+"/api/broker/observation")).status,401);
const login=await fetch(origin+"/api/session",{method:"POST",headers:{origin,"Content-Type":"application/json"},body:JSON.stringify({secret:process.env.PRAMANA_DASHBOARD_SECRET})});
assert.equal(login.status,200);const cookie=login.headers.get("set-cookie")?.split(";")[0];assert(cookie);
const request=(route,body)=>fetch(origin+route,{method:body?"POST":"GET",headers:{cookie,origin,"Content-Type":"application/json"},...(body?{body:JSON.stringify(body)}:{})});
const workspace=await(await request("/api/workspace")).json(),state=workspace.brokerObservation;
assert.equal(workspace.liveEnabled,false);assert.equal(workspace.checks.find(c=>c.id==="evidence").pass,false);
assert.equal(state.status,"available",state.detail);assert.equal(state.journal.captureCount,6);assert.equal(state.journal.selectedSequence,6);assert.equal(state.inspection.orderCount,14);assert.equal(state.inspection.tradeCount,27);assert.equal(state.journal.temporal.issueCount,0);assert(state.journal.cumulativeIssueCount>0);
const journalId=state.journal.journalId,selected=[];
for(let sequence=1;sequence<=6;sequence++) {
 const response=await request(`/api/broker/observation?journalId=${journalId}&sequence=${sequence}`);
 assert.equal(response.status,200);assert.equal(response.headers.get("Cache-Control"),"no-store");assert.match(response.headers.get("Content-Disposition"),/attachment/);
 const s=await response.json();assert.equal(s.journal.selectedSequence,sequence);selected.push(s);
}
assert.equal(selected[0].journal.temporal.status,"baseline");assert.equal(selected[1].journal.temporal.status,"compared");
assert(selected[2].journal.temporal.issues.some(i=>i.code==="filled_quantity_regressed"));
for(const index of [3,4])assert(selected[index].journal.temporal.issues.some(i=>i.code==="previously_observed_trade_missing"));
assert.equal((await request(`/api/broker/observation?journalId=${journalId}&sequence=2&captureSha256=${"0".repeat(64)}`)).status,503);
const brokerCapture={journalId,sequence:1,captureSha256:selected[0].report.sha256};
const answer=await(await request("/api/copilot",{prompt:"Explain this selected broker capture and its preceding lifecycle evidence.",brokerCapture})).json();
assert.equal(answer.status,"error");assert.match(answer.error,/Claude is not configured/);
const context=JSON.parse(answer.context);assert.equal(context.mode,"broker_capture_review");assert.equal(context.brokerObservation.lifecycle.sequence,1);assert.equal(context.brokerObservation.lifecycle.cumulativeIssueCount,0);assert.equal(context.portfolio,undefined);assert.equal(context.market,undefined);assert.equal(context.brokerObservation.lifecycle.temporal.changes,undefined);
for(const text of [state.report.accountRef,"trade-13-b","synthetic-13","headHash"])assert(!JSON.stringify(context).includes(text));
console.log(JSON.stringify({phase:process.env.BROKER_HISTORY_PHASE||"broker-history",status:"pass",journalId,headHash:state.journal.headHash,captures:6,selectedExports:6,orders:14,executions:27,cumulativeIssueCount:state.journal.cumulativeIssueCount,partialCompletion:true,persistentMissingRecords:true,historyPreservedAfterRecovery:true,selectedAtlasContext:true,futureContextExcluded:true,modelCalls:0,brokerCalls:0,liveEnabled:false,operatorAcceptance:false}));
