import test from "node:test";
import assert from "node:assert/strict";
import { portfolioRisk } from "../lib/portfolio-risk";
import type { Portfolio } from "../lib/types";
function portfolio(): Portfolio {
 return {status:"ok",markMode:"engine_live",markDisclaimer:"test",cash:500,totalEquity:1000,
 realizedPnl:0,unrealizedPnl:0,highWaterMark:1000,drawdown:0,equityCurve:[],updatedAt:null,
 holdings:[{symbol:"A",market:"INDIA",assetClass:"EQUITY",quantity:3,averageEntry:100,markPrice:100,marketValue:300,unrealizedPnl:0,markSource:"test",fresh:true,stopPrice:90},
 {symbol:"B",market:"INDIA",assetClass:"ETF",quantity:2,averageEntry:100,markPrice:100,marketValue:200,unrealizedPnl:0,markSource:"test",fresh:false}]};
}
test("risk shocks use equity weights including cash and aggregate distinct shocks",()=>{
 const r=portfolioRisk(portfolio(),-10,{"INDIA:ETF:B":5});assert.equal(r.status,"ok");if(r.status!=="ok")return;
 assert.equal(r.pnl,-20);assert.equal(r.scenarioEquity,980);assert.equal(r.equityImpact,-.02);
 assert.equal(r.largestEquityWeight,.3);assert.equal(r.grossEquityWeight,.5);
 assert.ok(Math.abs(r.effectiveHoldings!-1/.52)<1e-12);
 assert.equal(r.recordedStopDownside,30);assert.equal(r.missingStops,1);assert.equal(r.staleMarks,1);
});
test("breached stops are warnings rather than negative loss or unlimited protection",()=>{
 const p=portfolio();p.holdings[0].stopPrice=110;
 const r=portfolioRisk(p,0);assert.equal(r.status,"ok");if(r.status!=="ok")return;
 assert.equal(r.breachedStops,1);assert.equal(r.recordedStopDownside,0);assert.equal(r.missingStops,1);
});
test("invalid geometry, contracts, mixed markets and duplicate holdings fail closed",()=>{
 for(const mutate of [(p:Portfolio)=>{p.totalEquity=0},(p:Portfolio)=>{p.holdings[0].marketValue=999},
 (p:Portfolio)=>{p.holdings[0].quantity=-1},(p:Portfolio)=>{p.holdings[0].assetClass="OPTION"},
 (p:Portfolio)=>{p.holdings[0].market="USA"},(p:Portfolio)=>{p.holdings.push(p.holdings[0])}]){
  const p=portfolio();mutate(p);assert.equal(portfolioRisk(p,-5).status,"unavailable");
 }
 for(const value of [NaN,Infinity,-101,101])assert.equal(portfolioRisk(portfolio(),0,{"INDIA:ETF:B":value}).status,"unavailable");
});
test("cash only has no concentration and removed overrides do not affect current holdings",()=>{
 const p=portfolio();p.holdings=[];const r=portfolioRisk(p,-50,{removed:-100});
 assert.equal(r.status,"ok");if(r.status!=="ok")return;
 assert.equal(r.pnl,0);assert.equal(r.effectiveHoldings,null);assert.equal(r.missingStops,0);
});
test("an expired portfolio cannot retain fresh labels from its old payload",()=>{
 const p=portfolio();p.status="stale";
 const r=portfolioRisk(p,-5);assert.equal(r.status,"ok");if(r.status!=="ok")return;
 assert.equal(r.staleMarks,2);assert.ok(r.rows.every(row=>!row.fresh));
});
