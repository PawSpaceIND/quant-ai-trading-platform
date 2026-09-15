// Production server over loopback, real paper-broker fixture, no provider/model calls.
import assert from "node:assert/strict";
import fs from "node:fs";
const origin=process.env.SMOKE_ORIGIN || "http://localhost:3000";
assert(["localhost","127.0.0.1"].includes(new URL(origin).hostname));
const fixture=JSON.parse(fs.readFileSync(process.env.BENCHMARK_FIXTURE || "/data/benchmark/fixture.json","utf8"));
assert.equal((await fetch(origin+"/api/portfolio/benchmark")).status,401);
const login=await fetch(origin+"/api/session",{method:"POST",headers:{origin,"Content-Type":"application/json"},body:JSON.stringify({secret:process.env.PRAMANA_DASHBOARD_SECRET})});
assert.equal(login.status,200);const cookie=login.headers.get("set-cookie")?.split(";")[0];assert(cookie);
const request=(route,body)=>fetch(origin+route,{method:body ? "POST" : "GET",headers:{cookie,origin,"Content-Type":"application/json"},...(body ? {body:JSON.stringify(body)} : {})});
const workspace=await(await request("/api/workspace")).json();
assert.equal(workspace.benchmarkPerformance.status,"available",workspace.benchmarkPerformance.detail);
assert.equal(workspace.liveEnabled,false);assert.equal(workspace.checks.find(c=>c.id==="evidence").pass,false);assert.equal(workspace.copilotConfigured,false);
const report=workspace.benchmarkPerformance.report, near=(a,b)=>assert(Math.abs(a-b)<1e-8,`${a} != ${b}`);
assert.equal(report.days.length,31);assert.equal(report.historicalFillCount,6);
report.days.forEach((d,i)=>{const expected=fixture.expected[i];assert.equal(d.date,expected.date);near(d.cash,expected.cash);near(d.accountEquity,expected.accountEquity);near(d.cashFees,expected.cashFees);});
const comparisons=[];
for(const benchmark of ["NIFTY 50","NIFTY BANK"])for(const lookback of [20,60,252]) {
  const response=await request(`/api/portfolio/benchmark?benchmark=${encodeURIComponent(benchmark)}&lookback=${lookback}`);
  assert.equal(response.status,200);assert.equal(response.headers.get("Cache-Control"),"no-store");assert.match(response.headers.get("Content-Disposition"),/attachment/);
  const exported=await response.json();assert.equal(exported.report.sourceSha256,report.sourceSha256);assert.equal(exported.comparison.complete,true);
  assert.equal(exported.comparison.intervals,Math.min(30,lookback));comparisons.push(exported.comparison);
}
assert.equal((await request("/api/portfolio/benchmark?lookback=0")).status,400);
const answer=await(await request("/api/copilot",{prompt:"Explain NIFTY BANK for up to 20 sessions using benchmarkPerformance evidence."})).json();
assert.equal(answer.status,"error");assert.match(answer.error,/Claude is not configured/);
const context=JSON.parse(answer.context);assert.equal(context.benchmarkPerformance.report.sourceSha256,report.sourceSha256);
assert.equal(context.benchmarkPerformance.report.days,undefined);assert.equal(context.benchmarkPerformance.report.comparisons.length,6);assert.equal(context.market.riskHistory,undefined);
for(const c of context.benchmarkPerformance.report.comparisons){const expected=comparisons.find(x=>x.benchmark===c.benchmark && x.lookback===c.lookback);near(c.accountReturn,expected.accountReturn);near(c.benchmarkReturn,expected.benchmarkReturn);assert.equal(c.days,undefined);assert.equal(c.curve,undefined);}
console.log(JSON.stringify({phase:"account-benchmark",status:"pass",authenticated:true,liveEnabled:false,
  oracleDays:31,recordedFills:6,comparisonExports:6,atlasSavedContext:true,modelCalls:0,operatorAcceptance:false,
  sourceSha256:report.sourceSha256,accountSha256:report.accountSha256,
  benchmarkReturns:comparisons.filter(c=>c.lookback===60).map(c=>({benchmark:c.benchmark,accountReturn:c.accountReturn,benchmarkReturn:c.benchmarkReturn}))}));
