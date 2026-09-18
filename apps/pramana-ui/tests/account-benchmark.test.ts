import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";
import {compareBenchmark,accountBenchmarkContext} from "../lib/benchmark-comparison";
import type {Input} from "../lib/daily-history";

const folder=fs.mkdtempSync(path.join(os.tmpdir(),"account-benchmark-")), file=path.join(folder,"paper.sqlite");
process.env.PRAMANA_TENANT_ID="default";process.env.PRAMANA_LEDGER_PATH=file;
process.env.PRAMANA_CONSOLE_DB=path.join(folder,"console.sqlite");process.env.PRAMANA_MARKET_SNAPSHOT=path.join(folder,"market.json");process.env.PRAMANA_PROOF_DIR=path.join(folder,"proofs");
const shared=JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/account-benchmark.json",import.meta.url),"utf8"));
const input=():Input=>structuredClone(shared.input), now=Date.parse(shared.valuation.updatedAt);
const near=(a:number|null,b:number)=>assert(a!==null && Math.abs(a-b)<1e-8,`${a} != ${b}`);
after(()=>fs.rmSync(folder,{recursive:true,force:true}));
function source() {
  fs.rmSync(file,{force:true}); const db=new DatabaseSync(file);
  db.exec(`PRAGMA journal_mode=WAL;
    CREATE TABLE paper_accounts(tenant_id TEXT PRIMARY KEY,starting_capital TEXT,cash_balance TEXT,updated_at TEXT);
    CREATE TABLE paper_ledger(id INTEGER PRIMARY KEY,order_id TEXT,tenant_id TEXT,symbol TEXT,market TEXT,asset_class TEXT,side TEXT,quantity INTEGER,fill_price TEXT,notional TEXT,status TEXT,created_at TEXT,stop_price TEXT,take_profit_price TEXT,margin_change TEXT,margin_provenance TEXT,instrument_identity TEXT);
    CREATE TABLE paper_cost_ledger(id INTEGER PRIMARY KEY,order_id TEXT,tenant_id TEXT,code TEXT,amount TEXT,cash_debit INTEGER,created_at TEXT);
    CREATE TABLE paper_positions(tenant_id TEXT,symbol TEXT,market TEXT,asset_class TEXT,quantity INTEGER,average_price TEXT,stop_price TEXT,take_profit_price TEXT,instrument_identity TEXT);
    CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,ledger_id INTEGER,payload TEXT);`);
  db.prepare("INSERT INTO paper_accounts VALUES ('default',?,?,?)").run(shared.account.starting_capital,shared.account.cash_balance,shared.valuation.updatedAt);
  for(const [table,rows] of [["paper_ledger",shared.fills],["paper_cost_ledger",shared.costs],["paper_positions",shared.positions]] as const)
    for(const row of rows)db.prepare(`INSERT INTO ${table} (${Object.keys(row).join(",")}) VALUES (${Object.keys(row).map(()=>"?").join(",")})`).run(...Object.values(row) as Array<string|number|null>);
  db.prepare("INSERT INTO paper_live_valuations VALUES ('default',?,6,?)").run(shared.valuation.updatedAt,JSON.stringify(shared.valuation));db.close();
}
function mutate(sql:string) {const db=new DatabaseSync(file);try{db.exec(sql);}finally{db.close();}}
function market(history:unknown) {fs.writeFileSync(process.env.PRAMANA_MARKET_SNAPSHOT!,JSON.stringify({status:"ok",fetchedAt:shared.input.asOf,rows:[],riskHistory:history}));}

test("real recorded fills match independent daily cash/equity/fee oracle, including closed losses",async t=>{
  source();t.mock.method(Date,"now",()=>now);
  const {readPortfolioSnapshot}=await import("../lib/portfolio");
  const state=readPortfolioSnapshot(input()).benchmarkPerformance;assert.equal(state.status,"available",state.detail);const r=state.report!;
  assert.equal(r.days.length,31);assert.equal(r.historicalFillCount,6);assert.equal(r.excludedLaterFillCount,0);
  r.days.forEach((d,i)=>{const expected=shared.expected[i];assert.equal(d.date,expected.date);near(d.cash,expected.cash);near(d.accountEquity,expected.accountEquity);near(d.cashFees,expected.cashFees);assert.equal(d.fillCount,expected.fillCount);});
  assert(r.days[10].holdings.some(h=>h.symbol==="INFY"));assert(!r.days.at(-1)!.holdings.some(h=>h.symbol==="INFY"));
  near(r.days.at(-1)!.cash,9644.39626);near(r.days.reduce((s,d)=>s+d.cashFees,0),1.30374);
  const c=compareBenchmark(r,"NIFTY 50",60);assert(c.complete);assert.equal(c.intervals,30);
  near(c.accountReturn,r.days.at(-1)!.accountEquity!/10000-1);near(c.benchmarkReturn,20400/20000-1);
  near(c.excessReturn,c.accountReturn!-c.benchmarkReturn!);near(c.relativeWealthReturn,(1+c.accountReturn!)/(1+c.benchmarkReturn!)-1);
  assert.notEqual(c.excessReturn,c.relativeWealthReturn);assert(c.trackingError!>0);assert(c.beta!==null);
  const short=compareBenchmark(r,"NIFTY BANK",20);assert.equal(short.intervals,20);assert.equal(short.from,r.days[10].date);assert.notEqual(short.benchmarkReturn,c.benchmarkReturn);
  assert.equal(r.qualification,"unqualified_price_comparison");assert.match(r.sourceSha256,/^[a-f0-9]{64}$/);
});

test("a missing historical close for an already closed holding withholds the affected range only",async t=>{
  source();t.mock.method(Date,"now",()=>now);const {readPortfolioSnapshot}=await import("../lib/portfolio");
  const h=input(), infy=h.instruments.find(i=>i.symbol==="INFY")!;infy.observations.splice(9,1);
  const state=readPortfolioSnapshot(h).benchmarkPerformance;assert.equal(state.status,"incomplete");const r=state.report!;
  assert.equal(r.days[9].accountEquity,null);assert.deepEqual(r.days[9].missingHoldings,["INFY · EQUITY"]);
  const c=compareBenchmark(r,"NIFTY 50",60);assert(!c.complete);assert.equal(c.accountReturn,null);assert.equal(c.beta,null);assert.equal(c.curve[9].account,null);assert.equal(c.intervals,30);
  assert(compareBenchmark(r,"NIFTY 50",20).complete);
  infy.observations=infy.observations.filter(o=>o.date!==r.days.at(-1)!.date);
  assert(compareBenchmark(readPortfolioSnapshot(h).benchmarkPerformance.report!,"NIFTY 50",20).complete,"Closed holding requires no later close");
  h.instruments.find(i=>i.symbol==="NIFTY 50")!.observations.shift();
  const missing=compareBenchmark(readPortfolioSnapshot(h).benchmarkPerformance.report!,"NIFTY 50",60);
  assert(!missing.complete);assert(missing.curve.every(d=>d.benchmark===null));assert.equal(missing.benchmarkReturn,null);
  assert(compareBenchmark(readPortfolioSnapshot(h).benchmarkPerformance.report!,"NIFTY BANK",20).complete);
});

test("malformed, ambiguous, stale, off-session and unreconciled inputs cannot generate comparison",async t=>{
  t.mock.method(Date,"now",()=>now);const {readPortfolioSnapshot}=await import("../lib/portfolio");
  for(const change of [
    (h:Input)=>h.instruments[0].observations[2].close=0,
    (h:Input)=>h.instruments[0].observations.reverse(),
    (h:Input)=>h.instruments[0].observations.push(h.instruments[0].observations[0]),
    (h:Input)=>h.instruments[0].currency="USD",
    (h:Input)=>h.instruments[0].exchange="BSE",
    (h:Input)=>h.instruments[1].providerInstrumentId=h.instruments[0].providerInstrumentId,
    (h:Input)=>h.calendar.sessions.reverse(),
    (h:Input)=>h.calendar.specialSessions=["2026-02-08"],
    (h:Input)=>h.asOf="2000-01-01T00:00:00Z",
    (h:Input)=>h.asOf=h.asOf.slice(0,19),
    (h:Input)=>h.priceBasis="adjusted",
  ]) {source();const h=input();change(h);const s=readPortfolioSnapshot(h).benchmarkPerformance;assert.equal(s.status,"invalid",String(change)+s.detail);assert.equal(s.report,null);}
  for(const sql of ["UPDATE paper_accounts SET cash_balance='0'","UPDATE paper_positions SET quantity=999 WHERE symbol='TCS'","UPDATE paper_cost_ledger SET amount='NaN' WHERE id=1",
    "UPDATE paper_ledger SET created_at=substr(created_at,1,10)||'T08:00:00+05:30' WHERE id=1; UPDATE paper_cost_ledger SET created_at=substr(created_at,1,10)||'T08:00:00+05:30' WHERE order_id=(SELECT order_id FROM paper_ledger WHERE id=1)",
    "UPDATE paper_ledger SET created_at=substr(created_at,1,19); UPDATE paper_cost_ledger SET created_at=substr(created_at,1,19)"]) {
    source();mutate(sql);assert.equal(readPortfolioSnapshot(input()).benchmarkPerformance.status,"invalid",sql);
  }
  source();assert.equal(readPortfolioSnapshot().benchmarkPerformance.status,"unavailable");
  fs.rmSync(file);assert.equal(readPortfolioSnapshot(input()).benchmarkPerformance.status,"unavailable");assert(!fs.existsSync(file));
});

test("history stops at the account valuation and retains fees/positions carried into a shorter window",async t=>{
  source();t.mock.method(Date,"now",()=>now);const {readPortfolioSnapshot}=await import("../lib/portfolio");
  const h=input(), snapshot=structuredClone(shared.valuation);
  // All fills are earlier than this cutoff. Do not extrapolate account history to newer source sessions.
  snapshot.updatedAt=shared.expected[25].date+"T12:00:00+05:30";
  snapshot.holdings.forEach((v:{markTimestamp:string})=>v.markTimestamp=snapshot.updatedAt);
  const db=new DatabaseSync(file);db.prepare("UPDATE paper_live_valuations SET timestamp=?,payload=?").run(snapshot.updatedAt,JSON.stringify(snapshot));db.close();
  const s=readPortfolioSnapshot(h).benchmarkPerformance;assert.equal(s.status,"available",s.detail);
  assert.equal(s.report!.days.at(-1)!.date,shared.expected[24].date);
  const short=compareBenchmark(s.report!,"NIFTY 50",20);assert.equal(short.days[0].date,shared.expected[4].date);near(short.days[0].cash,shared.expected[4].cash);assert.equal(short.days[0].holdings[0].quantity,6);
  const noBaseline=input();noBaseline.calendar.sessions=noBaseline.calendar.sessions.filter(d=>d>=shared.expected[1].date);noBaseline.instruments.forEach(i=>i.observations=i.observations.slice(1));
  assert.equal(readPortfolioSnapshot(noBaseline).benchmarkPerformance.status,"unavailable");
});

test("relative-risk formulas match a known beta-one path, and constant/short series remain explicit",async t=>{
  source();t.mock.method(Date,"now",()=>now);const {readPortfolioSnapshot}=await import("../lib/portfolio");
  const r=readPortfolioSnapshot(input()).benchmarkPerformance.report!;
  r.days.forEach(d=>d.accountEquity=d.benchmarkCloses["NIFTY 50"]!/2);
  let c=compareBenchmark(r,"NIFTY 50",60);near(c.beta,1);near(c.correlation,1);near(c.trackingError,0);assert.equal(c.informationRatio,null);near(c.relativeWealthReturn,0);
  r.days.forEach(d=>{d.accountEquity=10000;d.benchmarkCloses["NIFTY 50"]=20000;});
  c=compareBenchmark(r,"NIFTY 50",60);assert.equal(c.beta,null);assert.equal(c.correlation,null);near(c.accountDrawdown,0);
  r.days=r.days.slice(-10);c=compareBenchmark(r,"NIFTY 50",20);assert(c.complete);assert.equal(c.intervals,9);assert.equal(c.trackingError,null);assert.match(c.statisticsReason,/At least 20/);
});

test("the 253-mark bound carries all earlier fills and fees instead of inventing a new account",async t=>{
  source();t.mock.method(Date,"now",()=>now);const {readPortfolioSnapshot}=await import("../lib/portfolio");
  const h=input(), dates:string[]=[];
  for(let stamp=Date.parse("2025-01-02");stamp<=Date.parse("2026-09-11");stamp+=86400000){const d=new Date(stamp);if(![0,6].includes(d.getUTCDay()))dates.push(d.toISOString().slice(0,10));}
  h.calendar.coverageStart="2025-01-01";h.calendar.sessions=dates;
  h.instruments.forEach(i=>i.observations=dates.map(date=>({date,close:i.symbol==="NIFTY" ? 80 : i.assetClass==="INDEX" ? 20000 : 100})));
  const db=new DatabaseSync(file);
  [1,3,6,9,14,18].forEach((index,i)=>{const at=dates[index]+"T10:00:00+05:30";db.prepare("UPDATE paper_ledger SET created_at=? WHERE id=?").run(at,i+1);db.prepare("UPDATE paper_cost_ledger SET created_at=? WHERE order_id=?").run(at,shared.fills[i].order_id);});db.close();
  const s=readPortfolioSnapshot(h).benchmarkPerformance;assert.equal(s.status,"available",s.detail);const r=s.report!;
  assert.equal(r.days.length,253);assert.equal(r.days[0].date,dates.at(-253));assert.equal(r.historicalFillCount,6);
  near(r.days[0].cash,9644.39626);near(r.days[0].accountEquity,10024.39626);assert.equal(r.days[0].fillCount,0);assert.equal(r.days[0].cashFees,0);
  assert.equal(r.days[0].holdings.find(h=>h.symbol==="TCS")!.quantity,3);near(compareBenchmark(r,"NIFTY 50",252).accountReturn,0);
});

test("a concurrent WAL fill cannot mix the benchmark history with the previously validated account",async t=>{
  source();t.mock.method(Date,"now",()=>now);const {readPortfolioSnapshot}=await import("../lib/portfolio");
  const writer=new DatabaseSync(file), original=DatabaseSync.prototype.prepare;let reads=0,committed=false;
  t.mock.method(DatabaseSync.prototype,"prepare",function(this:DatabaseSync,sql:string){
    if(sql==="SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id LIMIT 20001" && ++reads===2){
      committed=true;writer.exec(`BEGIN;
        INSERT INTO paper_ledger (id,order_id,tenant_id,symbol,market,asset_class,side,quantity,fill_price,notional,status,created_at,stop_price,take_profit_price) VALUES(7,'concurrent','default','TCS','INDIA','EQUITY','BUY',1,'100','100','FILLED','2026-09-14T11:59:59+05:30',NULL,NULL);
        INSERT INTO paper_cost_ledger VALUES(100,'concurrent','default','SPREAD','0',0,'2026-09-14T11:59:59+05:30');
        INSERT INTO paper_cost_ledger VALUES(101,'concurrent','default','SLIPPAGE','0',0,'2026-09-14T11:59:59+05:30');
        UPDATE paper_accounts SET cash_balance='9544.39626'; UPDATE paper_positions SET quantity=4,average_price='105.88' WHERE symbol='TCS'; COMMIT;`);
    }
    return original.call(this,sql);
  });
  try {
    const first=readPortfolioSnapshot(input()).benchmarkPerformance;assert(committed);assert.equal(first.status,"available",first.detail);assert.equal(first.report!.ledgerId,6);assert.equal(first.report!.historicalFillCount,6);near(first.report!.days.at(-1)!.cash,9644.39626);
    const second=readPortfolioSnapshot(input()).benchmarkPerformance;assert.equal(second.status,"invalid");assert.equal(second.report,null);
  }finally{writer.close();}
});

test("workspace, private selected export and saved Atlas context share identical bounded comparisons",async t=>{
  source();t.mock.method(Date,"now",()=>now);market(input());
  const {GET}=await import("../app/api/portfolio/benchmark/route"), request=(q="")=>GET(new Request("http://localhost/api/portfolio/benchmark"+q));
  for(const q of ["?benchmark=SPX","?lookback=060","?lookback=10","?benchmark=NIFTY+50&benchmark=NIFTY+BANK"])assert.equal((await request(q)).status,400);
  const response=await request("?benchmark=NIFTY+BANK&lookback=20");assert.equal(response.status,200);assert.equal(response.headers.get("cache-control"),"no-store");assert.match(response.headers.get("content-disposition")!,/attachment/);
  const exported=await response.json();assert.equal(exported.comparison.benchmark,"NIFTY BANK");assert.equal(exported.comparison.intervals,20);
  const {GET:workspace}=await import("../app/api/workspace/route");const w=await(await workspace()).json();assert.equal(w.benchmarkPerformance.report.sourceSha256,exported.report.sourceSha256);
  delete process.env.PRAMANA_PORTFOLIO_RISK_METADATA;
  delete process.env.PRAMANA_PORTFOLIO_RISK_METADATA_FILE;
  const {GET:attribution}=await import("../app/api/portfolio/attribution/route");
  const attributionResponse=await attribution();assert.equal(attributionResponse.status,422);assert.equal(attributionResponse.headers.get("cache-control"),"no-store");assert.equal(attributionResponse.headers.get("content-disposition"),null);
  const {generateAnswer,conversations}=await import("../lib/copilot");delete process.env.ANTHROPIC_API_KEY;
  const answer=await generateAnswer("Explain NIFTY BANK over 20 sessions",undefined,"account-benchmark-fixture");assert.equal(answer.status,"error");
  const context=JSON.parse(conversations(answer.id)[0].context!);const saved=context.benchmarkPerformance;
  assert.equal(saved.report.sourceSha256,exported.report.sourceSha256);assert.equal(saved.report.comparisons.length,6);assert.equal(saved.report.days,undefined);assert.equal(saved.report.omittedDailyRows,31);
  const c=saved.report.comparisons.find((c:{benchmark:string;lookback:number})=>c.benchmark==="NIFTY BANK" && c.lookback===20);assert.equal(c.accountReturn,exported.comparison.accountReturn);assert.equal(c.benchmarkReturn,exported.comparison.benchmarkReturn);assert.equal(c.curve,undefined);assert.equal(c.days,undefined);
  assert.equal(context.market.riskHistory,undefined);assert.deepEqual(accountBenchmarkContext(w.benchmarkPerformance).report,saved.report);
  mutate("UPDATE paper_accounts SET cash_balance='0'");assert.equal((await request()).status,503);
  source();market(null);assert.equal((await request()).status,422);
});
