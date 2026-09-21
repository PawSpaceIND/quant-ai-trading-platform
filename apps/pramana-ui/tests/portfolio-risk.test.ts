import test from "node:test";
import assert from "node:assert/strict";
import { applyShockEdit, dailyLossUsed, portfolioRisk } from "../lib/portfolio-risk";
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

test("downside to recorded stops is withheld, not zero, when no holding has a stop",()=>{
 const p=portfolio();delete p.holdings[0].stopPrice;
 const r=portfolioRisk(p,0);assert.equal(r.status,"ok");if(r.status!=="ok")return;
 // Every stop missing means the downside is unknown. Zero would read as "no downside".
 assert.equal(r.recordedStopDownside,null);assert.equal(r.missingStops,2);
 // A breached stop still contributes a real zero, so a mixed book keeps a number.
 const mixed=portfolio();mixed.holdings[0].stopPrice=110;
 const m=portfolioRisk(mixed,0);assert.equal(m.status,"ok");if(m.status!=="ok")return;
 assert.equal(m.recordedStopDownside,0);
});

test("the daily-loss breaker reproduces the engine's own arithmetic",()=>{
 // The daemon computes opening = total_equity - daily_total_pnl and halts when
 // -daily_total_pnl / opening >= the published limit. The page drew no bar for this at
 // all, although breaching it engages the kill switch. Drawing a different line from the
 // one that halts trading would be worse than drawing none, so the arithmetic is pinned.
 const at=(totalEquity:number,dailyPnl:number)=>dailyLossUsed({totalEquity,dailyPnl});
 // A 2% loss on 100000 of opening equity: equity is 98000 and the day is -2000.
 assert.equal(at(98000,-2000),0.02);
 assert.equal(at(99000,-1000),0.01);
 // A profitable day consumes none of the breaker, and never reads as a negative fraction.
 assert.equal(at(101000,1000),0);
 // No opening equity means no denominator. The engine does not test the breaker here.
 assert.equal(at(0,0),null);
 assert.equal(at(1000,1000),null);
 assert.equal(at(500,1000),null);
 // Withheld totals publish a null dailyPnl; that is unknown, not a flat day.
 assert.equal(dailyLossUsed({totalEquity:98000,dailyPnl:undefined}),null);
 assert.equal(at(NaN,-2000),null);
 assert.equal(at(98000,NaN),null);
});

test("an emptied per-holding shock box falls back to the common shock rather than pinning zero",()=>{
 // Number("") is 0, so clearing a box to retype used to write an explicit 0% override:
 // the holding silently left the scenario and the box snapped to 0. Removing the key is
 // what "no override" means to portfolioRisk, so the fix is observable right here.
 const key="INDIA:ETF:B";
 // The edit rule itself: the old handler wrote 0 for every one of these.
 for(const raw of ["",""," ","-","abc","1e"])assert.deepEqual(applyShockEdit({[key]:25},key,raw),{},`"${raw}" must clear the override`);
 assert.deepEqual(applyShockEdit({},key,""),{},"clearing an unset box leaves it unset");
 assert.deepEqual(applyShockEdit({},key,"-7"),{[key]:-7});
 assert.deepEqual(applyShockEdit({},key,"250"),{[key]:100},"out of range still clamps");
 assert.deepEqual(applyShockEdit({},key,"-250"),{[key]:-100});
 assert.deepEqual(applyShockEdit({},key,"0"),{[key]:0},"a typed zero is still a real override");
 // Other holdings' overrides are untouched by an edit to one box.
 assert.deepEqual(applyShockEdit({"INDIA:EQUITY:A":5,[key]:25},key,""),{"INDIA:EQUITY:A":5});
 const pinned=portfolioRisk(portfolio(),-10,{[key]:0});
 assert.equal(pinned.status,"ok");if(pinned.status!=="ok")return;
 assert.equal(pinned.rows.find(r=>r.key===key)!.shock,0);
 assert.equal(pinned.pnl,-30);
 const cleared=portfolioRisk(portfolio(),-10,{});
 assert.equal(cleared.status,"ok");if(cleared.status!=="ok")return;
 assert.equal(cleared.rows.find(r=>r.key===key)!.shock,-10);
 assert.equal(cleared.pnl,-50);
});
