import {after,test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";

// A recorded capital contribution changes the starting capital the valuations carry. The
// performance panel must read it as a deposit, not a return and not tampering, while an
// unexplained change still invalidates the observations.
const folder=fs.mkdtempSync(path.join(os.tmpdir(),"capital-contribution-"));
process.env.PRAMANA_TENANT_ID="default";
process.env.PRAMANA_LEDGER_PATH=path.join(folder,"ledger.sqlite");
const db=new DatabaseSync(process.env.PRAMANA_LEDGER_PATH);
db.exec("CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,payload TEXT,PRIMARY KEY(tenant_id,timestamp))");
after(()=>{db.close();fs.rmSync(folder,{recursive:true,force:true});});

const dates:string[]=[];
for(let i=0;dates.length<30;i++) {const d=new Date(Date.UTC(2000,0,3+i));if(![0,6].includes(d.getUTCDay()))dates.push(d.toISOString().slice(0,10));}
const TOPUP_DAY=15, DEPOSIT=900000;
// After day 14's close (16:00 IST), outside NSE hours as the engine requires.
const contributedAt=`${dates[TOPUP_DAY-1]}T10:30:00.000Z`;
const equity=(day:number)=>day<TOPUP_DAY?100000+100*day:100000+100*day+DEPOSIT;
const capital=(day:number)=>day<TOPUP_DAY?100000:100000+DEPOSIT;
const insert=db.prepare("INSERT INTO paper_live_valuations VALUES ('default',?,?)");

function seed(capitalOf=capital,equityOf=equity) {
 db.exec("DELETE FROM paper_live_valuations; BEGIN");
 for(const [day,date] of dates.entries())for(let i=0;i<300;i++) {
  const at=new Date(Date.parse(`${date}T05:00:00Z`)+i*60000).toISOString();
  insert.run(at,JSON.stringify({updatedAt:at,sessionDate:date,previousSessionDate:dates[day-1]??"1999-12-31",
   status:"ok",allMarksFresh:true,qualifyingSession:true,startingCapital:capitalOf(day),totalEquity:equityOf(day)}));
 }
 db.exec("COMMIT");
}
function record(rows:Array<[string,number,number]>) {
 db.exec("DROP TABLE IF EXISTS paper_capital_contributions");
 db.exec("CREATE TABLE paper_capital_contributions(tenant_id TEXT,reference TEXT,contributed_at TEXT,capital_before TEXT,capital_after TEXT)");
 const add=db.prepare("INSERT INTO paper_capital_contributions VALUES ('default',?,?,?,?)");
 rows.forEach(([at,before,afterCapital],i)=>add.run(`topup-${i}`,at,String(before),String(afterCapital)));
}

test("a recorded contribution is a deposit: every session still counts and no day earns it",async()=>{
 seed();record([[contributedAt,100000,1000000]]);
 const {performance}=await import("../lib/pilot");
 const p=performance();
 assert.equal(p.status,"observed");assert.equal(p.days,30);
 const expected=(equity(TOPUP_DAY-1)/100000)*((equity(TOPUP_DAY)-DEPOSIT)/equity(TOPUP_DAY-1))*(equity(29)/equity(TOPUP_DAY))-1;
 assert(Math.abs(p.netReturn!-expected)<1e-12,`${p.netReturn} vs ${expected}`);
 assert(p.netReturn!<0.02,"the deposit must not read as a return");
});

test("without a record the same step is still an unexplained capital change",async()=>{
 seed();db.exec("DROP TABLE IF EXISTS paper_capital_contributions");
 const {performance}=await import("../lib/pilot");
 assert.equal(performance().status,"invalid_observations");
});

test("a valuation that claims the new capital before the deposit is refused",async()=>{
 seed((day)=>day<TOPUP_DAY-1?100000:100000+DEPOSIT);record([[contributedAt,100000,1000000]]);
 const {performance}=await import("../lib/pilot");
 assert.equal(performance().status,"invalid_observations");
});

test("a recorded amount that does not match the valuations is refused",async()=>{
 seed();record([[contributedAt,100000,900000]]);
 const {performance}=await import("../lib/pilot");
 assert.equal(performance().status,"invalid_observations");
});

test("contribution records that do not continue from one another are refused",async()=>{
 // Each second record leaves the valuations' capital schedule intact, so only the record
 // check itself can refuse it.
 const later=`${dates[20]}T10:30:00.000Z`;
 seed();record([[contributedAt,100000,1000000],[later,500000,1000000]]);
 const {performance}=await import("../lib/pilot");
 assert.equal(performance().status,"invalid_observations");
 record([[contributedAt,100000,1000000],[later,1000000,1000000]]);
 assert.equal(performance().status,"invalid_observations");
 record([[contributedAt,100000,1000000]]);
 assert.equal(performance().status,"observed");
});

test("daily returns are time-weighted: the deposit day earns only its trading",async()=>{
 seed();record([[contributedAt,100000,1000000]]);
 const {performance}=await import("../lib/pilot");
 const returns=dates.slice(1).map((_,k)=>{const i=k+1;return (equity(i)-(i===TOPUP_DAY?DEPOSIT:0))/equity(i-1)-1;});
 const mean=returns.reduce((a,b)=>a+b,0)/returns.length;
 const sd=Math.sqrt(returns.reduce((a,b)=>a+(b-mean)**2,0)/(returns.length-1));
 const p=performance();
 assert(Math.abs(p.sharpe!-(mean/sd)*Math.sqrt(252))<1e-9,`${p.sharpe}`);
});

test("with no contribution the net return is unchanged: last close over starting capital",async()=>{
 seed(()=>100000,(day)=>100000+100*day);db.exec("DROP TABLE IF EXISTS paper_capital_contributions");
 const {performance}=await import("../lib/pilot");
 const p=performance();
 assert.equal(p.status,"observed");
 assert(Math.abs(p.netReturn!-(100000+100*29)/100000+1)<1e-12);
});
