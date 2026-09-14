import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";

const folder=fs.mkdtempSync(path.join(os.tmpdir(),"observation-history-"));
process.env.PRAMANA_TENANT_ID="default";
process.env.PRAMANA_LEDGER_PATH=path.join(folder,"ledger.sqlite");
const db=new DatabaseSync(process.env.PRAMANA_LEDGER_PATH);
db.exec("CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,payload TEXT,PRIMARY KEY(tenant_id,timestamp))");
after(()=>{db.close();fs.rmSync(folder,{recursive:true,force:true});});
const sha="a".repeat(64),coverageStart="2000-01-01T00:00:00Z";
const dates:string[]=[];
for(let i=0;dates.length<30;i++) {const d=new Date(Date.UTC(2000,0,3+i));if(![0,6].includes(d.getUTCDay()))dates.push(d.toISOString().slice(0,10));}
const insert=db.prepare("INSERT INTO paper_live_valuations VALUES ('default',?,?)");
function payload(timestamp:string,day:number,eligible=true) {
 return {updatedAt:timestamp,sessionDate:new Date(Date.parse(timestamp)+330*60000).toISOString().slice(0,10),
  previousSessionDate:dates[day-1]??"1999-12-31",status:"ok",allMarksFresh:eligible,qualifyingSession:eligible,
  startingCapital:100000,totalEquity:100000+day*100,strategyObservation:{manifestSha256:sha,eligible}};
}
function seed() {
 db.exec("DELETE FROM paper_live_valuations; BEGIN");
 for(let i=0;i<43200;i++) {const at=new Date(Date.parse("1999-11-01T00:00:00Z")+i*60000).toISOString();insert.run(at,JSON.stringify(payload(at,0,false)));}
 for(const [day,date] of dates.entries())for(let i=0;i<300;i++) {const at=new Date(Date.parse(`${date}T05:00:00Z`)+i*60000).toISOString();insert.run(at,JSON.stringify(payload(at,day)));}
 db.exec("COMMIT");
}

test("account and strategy qualification stream thirty complete sessions beyond 50,000 retained observations",async()=>{
 seed();const {performance}=await import("../lib/pilot");const {strategyObservationDays}=await import("../lib/strategy-evidence");
 assert.equal((db.prepare("SELECT count(*) AS n FROM paper_live_valuations").get() as {n:number}).n,52200);
 const account=performance();assert.equal(account.days,30);assert.equal(account.status,"observed");
 assert(account.daily.every(d=>d.minutes===300));assert(Math.abs(account.netReturn!-.029)<1e-12);
 assert.equal(strategyObservationDays(sha,[],coverageStart).days,30);
});

test("timezone aliases cannot inflate observations or choose a conflicting session close",async()=>{
 const {performance}=await import("../lib/pilot");const {strategyObservationDays}=await import("../lib/strategy-evidence");
 const at=`${dates[0]}T09:59:00.000Z`,row=db.prepare("SELECT payload FROM paper_live_valuations WHERE timestamp=?").get(at) as {payload:string};
 const alias=`${dates[0]}T15:29:00+05:30`;
 insert.run(alias,JSON.stringify({...JSON.parse(row.payload),totalEquity:999999}));
 assert.equal(performance().days,29);assert.equal(strategyObservationDays(sha,[],coverageStart).days,29);
 db.prepare("DELETE FROM paper_live_valuations WHERE timestamp=?").run(alias);
 assert.equal(performance().days,30);
 const earlier=`${dates[0]}T09:00:00.000Z`,laterLexically=`${dates[0]}T14:30:00+05:30`;
 const saved=db.prepare("SELECT payload FROM paper_live_valuations WHERE timestamp=?").get(earlier) as {payload:string};
 db.prepare("UPDATE paper_live_valuations SET timestamp=?,payload=? WHERE timestamp=?").run(laterLexically,JSON.stringify({...JSON.parse(saved.payload),totalEquity:222222}),earlier);
 assert.equal(performance().daily[0].equity,100000);assert.equal(strategyObservationDays(sha,[],coverageStart).days,30);
 db.prepare("UPDATE paper_live_valuations SET timestamp=?,payload=? WHERE timestamp=?").run(earlier,saved.payload,laterLexically);
 const impossible="2000-02-30T05:00:00Z";
 insert.run(impossible,JSON.stringify(payload(impossible,0)));
 assert.equal(performance().status,"invalid_observations");assert.equal(strategyObservationDays(sha,[],coverageStart).days,0);
 db.prepare("DELETE FROM paper_live_valuations WHERE timestamp=?").run(impossible);
});

test("late invalid observations remove an already accumulated day and inconsistent clocks fail closed",async()=>{
 const {performance}=await import("../lib/pilot");const {strategyObservationDays}=await import("../lib/strategy-evidence");
 const at=`${dates[0]}T10:00:00.000Z`;
 insert.run(at,JSON.stringify({...payload(at,0,false),status:"invalid",totalEquity:null}));
 assert.equal(performance().days,29);assert.equal(strategyObservationDays(sha,[],coverageStart).days,29);
 db.prepare("DELETE FROM paper_live_valuations WHERE timestamp=?").run(at);
 const original=`${dates[0]}T05:00:00.000Z`;
 for(const bad of [`${dates[0]}T05:00:00.000001Z`,`${dates[0]}T05:00:01Z`,`${dates[0]}T05:00:00`,`${dates[0]}T04:59:00Z`]) {
  db.prepare("UPDATE paper_live_valuations SET timestamp=? WHERE timestamp=?").run(bad,original);
  const p=performance();assert.equal(p.status,"invalid_observations");assert.equal(p.netReturn,null);
  assert.equal(strategyObservationDays(sha,[],coverageStart).days,0);
  db.prepare("UPDATE paper_live_valuations SET timestamp=? WHERE timestamp=?").run(original,bad);
 }
 assert.equal(performance().days,30);
 const saved=db.prepare("SELECT payload FROM paper_live_valuations WHERE timestamp=?").get(original) as {payload:string};
 db.prepare("UPDATE paper_live_valuations SET payload=? WHERE timestamp=?").run(JSON.stringify({...JSON.parse(saved.payload),startingCapital:99999}),original);
 assert.equal(performance().status,"invalid_observations");assert.equal(performance().netReturn,null);
 db.prepare("UPDATE paper_live_valuations SET payload=? WHERE timestamp=?").run(saved.payload,original);
});
