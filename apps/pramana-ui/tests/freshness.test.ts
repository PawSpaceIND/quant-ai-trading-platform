import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";
import {sourceAge,ageRuntime,tradingFeedCheck,agePortfolio,ageWorkspace} from "../lib/freshness";
import type {Runtime} from "../lib/pilot";
import type {Portfolio,Workspace} from "../lib/types";

const now=Date.parse("2026-09-15T06:00:00Z"),stamp=(n=now)=>new Date(n).toISOString();
const instrument={symbol:"INFY",currency:"INR",market:"INDIA",assetClass:"EQUITY",exchange:"NSE",fresh:true,tickTimestamp:stamp()};
const runtime=():Runtime=>({status:"running",mode:"paper",updatedAt:stamp(),watchlist:[{...instrument}],
 strategyManifest:{status:"matched",checkedAt:stamp(),sourceCheckAgeSeconds:0,issues:[]},
 reconciliation:{status:"matched",checkedAt:stamp(),ledgerId:0,issueCount:0,scope:"paper"}});
const portfolio=():Portfolio=>({status:"ok",markMode:"engine_live",markDisclaimer:"Source tick valuation",updatedAt:stamp(),cash:99900,totalEquity:100000,realizedPnl:0,unrealizedPnl:0,highWaterMark:100000,drawdown:0,equityCurve:[],holdings:[{symbol:"INFY",market:"INDIA",assetClass:"EQUITY",quantity:1,averageEntry:100,markPrice:100,marketValue:100,unrealizedPnl:0,markSource:"live_tick",markTimestamp:stamp(),fresh:true}]});

test("source ages preserve microsecond boundaries and reject missing offsets and impossible dates",()=>{
 assert.equal(sourceAge("2026-09-15T11:30:00+05:30",now),0);
 assert.equal(sourceAge("2026-09-15T05:57:59.999999Z",now),120.000001);
 assert.equal(sourceAge("2026-09-15T06:00:00.000001Z",now),-.000001);
 for(const bad of [undefined,null,"2026-09-15T06:00:00","2026-02-30T06:00:00Z","2026-09-15T24:00:00Z","0000-01-01T00:00:00Z","2026-09-15T06:00:00.0000001Z"])assert.equal(sourceAge(bad,now),null,String(bad));
});

test("stored fresh flags cannot survive expired heartbeats, missing, future or expired source ticks",()=>{
 const good=runtime();assert(tradingFeedCheck(good,now).pass);assert(tradingFeedCheck(good,now+10000).pass);
 assert(!tradingFeedCheck(good,now+10001).pass);assert.equal(ageRuntime(good,now+10001).watchlist![0].fresh,false);
 for(const tick of [undefined,"2026-09-15T06:00:00.000001Z","2026-09-15T05:57:59.999999Z"]){const r=runtime();r.watchlist![0].tickTimestamp=tick;assert(!tradingFeedCheck(r,now).pass);}
 const edge=runtime();edge.watchlist![0].tickTimestamp="2026-09-15T05:58:00Z";assert(tradingFeedCheck(edge,now).pass);
 const rejected=runtime();rejected.watchlist![0].fresh=false;assert(!tradingFeedCheck(rejected,now).pass);
 assert.equal(good.watchlist![0].fresh,true); // No source mutation.
});

test("duplicate or malformed watchlists do not claim complete feed coverage",()=>{
 const r=runtime();r.watchlist!.push({...instrument});const state=ageRuntime(r,now);
 assert(state.watchlist!.every(i=>i.freshnessReason==="duplicate_instrument"&&!i.fresh));assert(!tradingFeedCheck(r,now).pass);
 for(const watchlist of [{fresh:true},[null],Array.from({length:501},(_,i)=>({...instrument,symbol:`S${i}`}))]) {
  const invalid=ageRuntime({...runtime(),watchlist} as Runtime,now);assert.equal(invalid.status,"invalid");assert.deepEqual(invalid.watchlist,[]);assert(!tradingFeedCheck(invalid,now).pass);
 }
});

test("held marks expire independently before the valuation snapshot expires",()=>{
 const p=portfolio();p.holdings[0].markTimestamp=stamp(now-119000);
 assert.equal(agePortfolio(p,now).status,"ok");const aged=agePortfolio(p,now+1001);
 assert.equal(aged.status,"degraded");assert.equal(aged.holdings[0].fresh,false);
 assert.equal(agePortfolio(p,now+30001).status,"stale");assert.equal(p.holdings[0].fresh,true);
 const future=portfolio();future.holdings[0].markTimestamp="2026-09-15T06:00:00.000001Z";assert.equal(agePortfolio(future,now).holdings[0].fresh,false);
});

test("cached workspace claims expire without polling and only new evidence recovers them",()=>{
 const base={generatedAt:stamp(),runtime:runtime(),portfolio:portfolio(),market:{status:"ok",fetchedAt:stamp(),rows:[],collectorStale:false},checks:["engine","ticks","quotes","marks","scope","recovery","strategy_manifest","evidence"].map(id=>({id,title:id,pass:true,detail:"recorded"})),historicalRisk:{status:"available",detail:"recorded",rows:[],report:{valuationAsOf:stamp()}},paperContribution:{status:"available",detail:"recorded",report:{marksCurrent:true,rows:[{market:"INDIA",assetClass:"EQUITY",symbol:"INFY",markState:"fresh"}]}},runComparison:{status:"published",detail:"historical",report:{id:"immutable"}}} as unknown as Workspace;
 base.attribution={status:"available",detail:"current",factors:[{name:"market",exposure:1}]};
 assert.equal(ageWorkspace(base,now).attribution?.status,"available");
 assert(ageWorkspace(base,now).checks.every(c=>c.pass));
 const stopped=ageWorkspace(base,now+10001);assert(!stopped.checks.find(c=>c.id==="engine")!.pass);assert(!stopped.checks.find(c=>c.id==="ticks")!.pass);
 const expired=ageWorkspace(base,now+30001);assert(expired.checks.every(c=>!c.pass));assert.equal(expired.portfolio.holdings[0].fresh,false);
 assert.equal(expired.attribution?.status,"unavailable");assert.equal(expired.attribution?.factors,undefined);
 assert.equal(expired.historicalRisk!.report,null);assert.equal(expired.paperContribution!.report,null);assert.equal(expired.runComparison,base.runComparison);
 assert.equal(ageWorkspace(expired,now).runtime.watchlist![0].fresh,false); // A clock rollback cannot resurrect flags.
 const recovered=structuredClone(base),later=now+31000;recovered.generatedAt=stamp(later);recovered.runtime.updatedAt=stamp(later);recovered.runtime.watchlist![0].tickTimestamp=stamp(later);recovered.portfolio.updatedAt=stamp(later);recovered.portfolio.holdings[0].markTimestamp=stamp(later);recovered.runtime.strategyManifest!.checkedAt=stamp(later);
 assert(ageWorkspace(recovered,later).checks.every(c=>c.pass));
 assert.equal(ageWorkspace(base,now+120001).market.collectorStale,true);
});

test("manifest and reconciliation source clocks expire even with a newer engine heartbeat",()=>{
 const base={generatedAt:stamp(),runtime:runtime(),portfolio:portfolio(),market:{status:"ok",fetchedAt:stamp(),rows:[]},checks:["strategy_manifest","reconciliation"].map(id=>({id,title:id,pass:true,detail:"recorded"}))} as unknown as Workspace;
 base.runtime.strategyManifest!.checkedAt=stamp(now-9999);base.runtime.reconciliation!.checkedAt=stamp(now-119999);
 assert(ageWorkspace(base,now).checks.every(c=>c.pass));assert(ageWorkspace(base,now+2).checks.every(c=>!c.pass));
});

const folder=fs.mkdtempSync(path.join(os.tmpdir(),"runtime-freshness-"));
process.env.PRAMANA_TENANT_ID="default";process.env.PRAMANA_LEDGER_PATH=path.join(folder,"paper.sqlite");
process.env.PRAMANA_CONSOLE_DB=path.join(folder,"console.sqlite");process.env.PRAMANA_MARKET_SNAPSHOT=path.join(folder,"missing-market.json");
const db=new DatabaseSync(process.env.PRAMANA_LEDGER_PATH);db.exec("CREATE TABLE pilot_runtime(tenant_id TEXT PRIMARY KEY,payload TEXT)");
after(()=>{db.close();fs.rmSync(folder,{recursive:true,force:true});});

test("runtime API and saved Atlas context re-evaluate source freshness rather than echo stored booleans",async()=>{
 const {readRuntime}=await import("../lib/pilot");const {GET}=await import("../app/api/workspace/route");const {generateAnswer,conversations}=await import("../lib/copilot");
 const r=runtime(),current=Date.now();r.updatedAt=stamp(current);r.watchlist![0].tickTimestamp=stamp(current-121000);
 db.prepare("INSERT OR REPLACE INTO pilot_runtime VALUES ('default',?)").run(JSON.stringify(r));
 assert.equal(readRuntime().watchlist![0].fresh,false);
 const w=await(await GET()).json();assert.equal(w.checks.find((c:{id:string})=>c.id==="ticks").pass,false);
 delete process.env.ANTHROPIC_API_KEY;const answer=await generateAnswer("Explain feed freshness",undefined,"freshness-context");
 const context=JSON.parse(conversations(answer.id)[0].context!);assert.equal(context.runtime.watchlist[0].fresh,false);assert.equal(context.runtime.watchlist[0].freshnessReason,"tick_expired");
 for(const bad of ["null",'{"status":"running"}',"x".repeat(1000001)]) {db.prepare("UPDATE pilot_runtime SET payload=?").run(bad);assert.equal(readRuntime().status,"invalid");}
});

test("broker and external-account observations expire with the workspace snapshot",()=>{
 // Both carry a server-side "newer than 120 seconds" verdict fixed at fetch time. Left
 // untouched they keep claiming a current observation beside an expired-evidence banner.
 const base={generatedAt:stamp(),runtime:runtime(),portfolio:portfolio(),
  market:{status:"ok",fetchedAt:stamp(),rows:[],collectorStale:false},checks:[]} as unknown as Workspace;
 base.brokerObservation={status:"available",detail:"Repeated broker order/trade reads only.",
  report:{finishedAt:stamp()},inspection:null} as unknown as Workspace["brokerObservation"];
 base.externalAccount={status:"available",detail:"Repeated selected-account reads only.",
  report:{finishedAt:stamp()}} as unknown as Workspace["externalAccount"];
 assert.equal(ageWorkspace(base,now).brokerObservation?.status,"available");
 assert.equal(ageWorkspace(base,now).externalAccount?.status,"available");
 const expired=ageWorkspace(base,now+130000);
 assert.equal(expired.brokerObservation?.status,"stale");
 assert.equal(expired.externalAccount?.status,"stale");
 assert.match(expired.brokerObservation!.detail,/older than 120 seconds/);
 assert.match(expired.externalAccount!.detail,/Historical selected-account snapshot/);
 // A state that was never available is not relabelled by ageing.
 const missing={...base,brokerObservation:{status:"unavailable",detail:"No selected broker observation.",
  report:null,inspection:null}} as unknown as Workspace;
 assert.equal(ageWorkspace(missing,now+130000).brokerObservation?.status,"unavailable");
});
