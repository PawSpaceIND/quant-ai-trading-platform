import {test,after} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";
import {historicalRisk,historicalRiskContext} from "../lib/historical-risk";
const shared=JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/historical-risk.json",import.meta.url),"utf8"));
const sample=()=>structuredClone(shared);
const now=Date.parse(shared.input.asOf);
const close=(a:number|null,b:number,tolerance=1e-10)=>assert(a!==null && Math.abs(a-b)<tolerance,`${a} != ${b}`);

test("independent Python orthogonal history reconciles portfolio covariance, contribution and historical tails",()=>{
  const {portfolio,input,expected}=sample();const state=historicalRisk(portfolio,input,now);
  assert.equal(state.status,"available",state.detail);const r=state.report!;
  assert.equal(r.intervals,120);close(r.cashWeight,.4);close(r.dailyVolatility**2,expected.variance);
  close(r.covariance[0][0],expected.covarianceDiagonal);close(r.covariance[0][1],0);
  close(r.correlation[0][1],0);close(r.correlation[0][0],1);assert.equal(r.correlation[2][2],null);
  close(r.rows[0].varianceShare,9/13);close(r.rows[1].varianceShare,4/13);close(r.rows[2].varianceShare,0);
  close(r.rows.reduce((s,x)=>s+x.volatilityContribution!,0),r.dailyVolatility);
  close(r.rows.reduce((s,x)=>s+x.varianceShare!,0),1);close(r.historicalLoss.var,50);close(r.historicalLoss.expectedShortfall,50);
  assert.deepEqual(r.scenarios.map(s=>Math.round(s.pnl)).sort((a,b)=>a-b),Array.from({length:30},()=>expected.scenarioPnlCycle).flat().sort((a:number,b:number)=>a-b));
  close(r.diversificationRatio, .5/Math.sqrt(.13));assert.equal(r.qualification,"exploratory");assert.match(r.sourceSha256,/^[a-f0-9]{64}$/);
});

test("cash dilution, perfect offsets and constant prices do not invent diversification",()=>{
  const {portfolio:p,input}=sample();p.cash+=1000;p.totalEquity+=1000;
  const diluted=historicalRisk(p,input,now).report!;close(diluted.historicalLoss.var,50);close(diluted.dailyVolatility,Math.sqrt(shared.expected.variance)/2);
  const offset=sample();offset.portfolio.holdings[0].quantity=2;offset.portfolio.holdings[0].marketValue=200;offset.portfolio.cash=500;
  let price=100;offset.input.instruments[1].observations.forEach((o:{close:number},t:number)=>{if(t)price*=1-([.1,-.1,.1,-.1][(t-1)%4]);o.close=price;});
  const hedged=historicalRisk(offset.portfolio,offset.input,now).report!;close(hedged.correlation[0][1],-1);close(hedged.dailyVolatility,0,1e-8);assert.equal(hedged.diversificationRatio,null);
  offset.portfolio.holdings[0].quantity=3;offset.portfolio.holdings[0].marketValue=300;offset.portfolio.cash=400;
  const partial=historicalRisk(offset.portfolio,offset.input,now).report!;close(partial.rows[0].varianceShare,3);close(partial.rows[1].varianceShare,-2);
  close(partial.rows.reduce((s,r)=>s+r.volatilityContribution!,0),partial.dailyVolatility);
  const flat=sample();flat.input.instruments.forEach((i:{observations:{close:number}[]})=>i.observations.forEach(o=>o.close=100));
  const f=historicalRisk(flat.portfolio,flat.input,now).report!;close(f.dailyVolatility,0);assert(f.correlation.every(row=>row.every(v=>v===null)));assert(f.rows.every(r=>r.varianceShare===null));
});

test("missing dates or instruments withhold aggregate risk without dropping uncovered exposures",()=>{
  for(const mutate of [(s:ReturnType<typeof sample>)=>s.input.instruments[0].observations.splice(4,1),(s:ReturnType<typeof sample>)=>s.input.instruments.pop()]){
    const s=sample();mutate(s);const result=historicalRisk(s.portfolio,s.input,now);
    assert.equal(result.status,"incomplete");assert.equal(result.report,null);assert(result.rows.some(r=>r.missingDates.length));
    close(result.rows.reduce((sum,r)=>sum+r.equityWeight,0),.6);
  }
  const s=sample();s.input.calendar.sessions=s.input.calendar.sessions.slice(-81);s.input.instruments.forEach((i:{observations:unknown[]})=>i.observations=i.observations.slice(-81));
  const r=historicalRisk(s.portfolio,s.input,now).report!;assert.equal(r.intervals,80);assert.equal(r.historicalLoss.var,null);assert.equal(r.historicalLoss.expectedShortfall,null);
  s.input.calendar.sessions=s.input.calendar.sessions.slice(-21);s.input.instruments.forEach((i:{observations:unknown[]})=>i.observations=i.observations.slice(-21));
  assert.equal(historicalRisk(s.portfolio,s.input,now).status,"incomplete");
});

test("invalid chronology, scope, provenance, valuation and malformed data fail closed",()=>{
  const changes=[
    (s:any)=>s.input.instruments[0].observations.push(s.input.instruments[0].observations[0]),
    (s:any)=>s.input.instruments[0].observations[2].close=0,
    (s:any)=>s.input.instruments[0].observations[2].close="100",
    (s:any)=>s.input.instruments[0].observations[2].close=true,
    (s:any)=>s.input.instruments[0].observations[2].close=Infinity,
    (s:any)=>s.input.instruments[0].observations[2].date="2026-02-30",
    (s:any)=>s.input.instruments[0].observations[2].date="2027-01-01",
    (s:any)=>s.input.instruments[0].currency="USD",
    (s:any)=>s.input.instruments[0].exchange="BSE",
    (s:any)=>s.input.instruments[1].providerInstrumentId=s.input.instruments[0].providerInstrumentId,
    (s:any)=>s.input.instruments.push(s.input.instruments[0]),
    (s:any)=>s.input.calendar.sessions.reverse(),
    (s:any)=>s.input.calendar.specialSessions=[],
    (s:any)=>s.input.calendar.specialSessions.push("2026-02-08"),
    (s:any)=>s.input.calendar.sessions.push(s.input.asOf.slice(0,10)),
    (s:any)=>s.input.calendar.coverageEnd="2025-12-31",
    (s:any)=>s.input.asOf="2000-01-01T00:00:00Z",
    (s:any)=>s.input.asOf=s.input.asOf.slice(0,19),
    (s:any)=>s.input.calendar.sessions=s.input.calendar.sessions.slice(0,80),
    (s:any)=>s.input.priceBasis="guaranteed_adjusted",
    (s:any)=>s.portfolio.holdings[0].fresh=false,
    (s:any)=>s.portfolio.holdings[0].markPrice=999,
    (s:any)=>s.portfolio.holdings[0].market="USA",
    (s:any)=>s.portfolio.holdings[0].assetClass="OPTION",
    (s:any)=>s.portfolio.holdings.push(s.portfolio.holdings[0]),
    (s:any)=>s.portfolio.totalEquity+=1,
    (s:any)=>s.portfolio.updatedAt="2000-01-01T00:00:00Z",
    (s:any)=>s.portfolio.status="stale",
  ];
  for(const mutate of changes){const s=sample();mutate(s);const result=historicalRisk(s.portfolio,s.input,now);assert.equal(result.status,"invalid",String(mutate)+': '+result.detail);assert.equal(result.report,null);}
  assert.equal(historicalRisk(shared.portfolio,null,now).status,"unavailable");
  assert.equal(historicalRisk(shared.portfolio,{status:"unavailable"},now).status,"unavailable");
  const s=sample();s.portfolio.holdings=[];s.portfolio.cash=1000;assert.match(historicalRisk(s.portfolio,s.input,now).detail,/Cash-only/);
});

test("95% expected shortfall uses fractional boundary weight and retains signed losses",()=>{
  const s=sample();s.input.calendar.sessions=s.input.calendar.sessions.slice(-102);s.input.instruments.forEach((i:{observations:unknown[]})=>i.observations=i.observations.slice(-102));
  // One invested series: 101 distinct, ascending gains. All observed losses are negative.
  s.portfolio.holdings=s.portfolio.holdings.slice(0,1);s.portfolio.cash=700;let price=100;
  s.input.instruments[0].observations.forEach((o:{close:number},t:number)=>{if(t)price*=1+t/10000;o.close=price;});
  const r=historicalRisk(s.portfolio,s.input,now).report!;
  close(r.historicalLoss.var,-.18);close(r.historicalLoss.expectedShortfall,-(.03+.06+.09+.12+.15+.05*.18)/5.05);
  assert(r.historicalLoss.worst<0);assert.equal(r.historicalLoss.sampleCount,101);
  const ctx=historicalRiskContext({status:"available",detail:"test",rows:r.rows,report:r});
  assert.equal(ctx.report!.worstScenarios.length,10);assert.equal(ctx.report!.omittedScenarios,91);assert(!("covariance" in ctx.report!));
});

const dir=fs.mkdtempSync(path.join(os.tmpdir(),"historical-risk-api-"));
after(()=>fs.rmSync(dir,{recursive:true,force:true}));
test("private API and Atlas preserve validated risk and exclude raw history from the model context",async t=>{
  process.env.PRAMANA_TENANT_ID="default";process.env.PRAMANA_CONSOLE_DB=path.join(dir,"console.sqlite");process.env.PRAMANA_LEDGER_PATH=path.join(dir,"paper.sqlite");
  process.env.PRAMANA_MARKET_SNAPSHOT=path.join(dir,"market.json");process.env.PRAMANA_PROOF_DIR=path.join(dir,"proofs");
  const paper=JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/paper-contribution.json",import.meta.url),"utf8"));
  const db=new DatabaseSync(process.env.PRAMANA_LEDGER_PATH);
  db.exec(`CREATE TABLE paper_accounts(tenant_id TEXT PRIMARY KEY,starting_capital TEXT,cash_balance TEXT,updated_at TEXT);
    CREATE TABLE paper_ledger(id INTEGER PRIMARY KEY,order_id TEXT,tenant_id TEXT,symbol TEXT,market TEXT,asset_class TEXT,side TEXT,quantity INTEGER,fill_price TEXT,notional TEXT,status TEXT,created_at TEXT,stop_price TEXT,take_profit_price TEXT,margin_change TEXT,margin_provenance TEXT,instrument_identity TEXT);
    CREATE TABLE paper_cost_ledger(id INTEGER PRIMARY KEY,order_id TEXT,tenant_id TEXT,code TEXT,amount TEXT,cash_debit INTEGER,created_at TEXT);
    CREATE TABLE paper_positions(tenant_id TEXT,symbol TEXT,market TEXT,asset_class TEXT,quantity INTEGER,average_price TEXT,stop_price TEXT,take_profit_price TEXT,instrument_identity TEXT);
    CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,ledger_id INTEGER,payload TEXT);`);
  db.prepare("INSERT INTO paper_accounts VALUES ('default',?,?,?)").run(paper.account.starting_capital,paper.account.cash_balance,shared.input.asOf);
  for(const [table,records] of [["paper_ledger",paper.fills],["paper_cost_ledger",paper.costs],["paper_positions",paper.positions]] as const){
    for(const row of records)db.prepare(`INSERT INTO ${table} (${Object.keys(row).join(",")}) VALUES (${Object.keys(row).map(()=>"?").join(",")})`).run(...Object.values(row) as Array<string|number|null>);
  }
  const valuation=paper.valuations.fresh;valuation.updatedAt=shared.input.asOf;valuation.holdings.forEach((h:{markTimestamp:string})=>h.markTimestamp=shared.input.asOf);
  db.prepare("INSERT INTO paper_live_valuations VALUES ('default',?,6,?)").run(shared.input.asOf,JSON.stringify(valuation));db.close();
  t.mock.method(Date,"now",()=>now);
  const input=sample().input;input.instruments=input.instruments.slice(0,2);input.instruments[0].symbol="NIFTY";input.instruments[0].assetClass="ETF";input.instruments[1].symbol="TCS";
  fs.writeFileSync(process.env.PRAMANA_MARKET_SNAPSHOT,JSON.stringify({status:"ok",fetchedAt:shared.input.asOf,rows:[],riskHistory:input}));
  const {GET}=await import("../app/api/portfolio/historical-risk/route");const response=await GET();assert.equal(response.status,200);assert.match(response.headers.get("Content-Disposition")!,/attachment/);assert.equal(response.headers.get("Cache-Control"),"no-store");const body=await response.json();close(body.report.historicalLoss.var,45.4);
  const {generateAnswer}=await import("../lib/copilot");delete process.env.ANTHROPIC_API_KEY;const answer=await generateAnswer("Risk sample",undefined,"risk-synthetic-context");assert.equal(answer.status,"error");
  const {conversations}=await import("../lib/copilot");const saved=conversations(answer.id)[0];const context=JSON.parse(saved.context!);assert.equal(context.historicalRisk.report.worstScenarios.length,10);assert.equal(context.market.riskHistory,undefined);assert.equal(context.historicalRisk.report.qualification,"exploratory");
  input.instruments[0].observations.pop();fs.writeFileSync(process.env.PRAMANA_MARKET_SNAPSHOT,JSON.stringify({rows:[],riskHistory:input}));assert.equal((await GET()).status,422);
});
