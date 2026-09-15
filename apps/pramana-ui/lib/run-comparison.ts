import fs from "node:fs";
import {createHash} from "node:crypto";
import {tenantId} from "./db";
import {regularMinute,metrics,groupFills,type RunComparisonReport,type RunComparisonState,type RunPoint} from "./run-comparison-model";
function check(ok:unknown):asserts ok {if(!ok)throw new Error("Invalid run comparison evidence");}
function exact(v:any,keys:string[]):void {check(v&&typeof v==="object"&&!Array.isArray(v)&&Object.keys(v).sort().join(",")===keys.sort().join(","));}
const hash=(v:unknown)=>typeof v==="string"&&/^[a-f0-9]{64}$/.test(v);
const date=(v:unknown)=>typeof v==="string"&&/^2026-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00$/.test(v)&&Number.isFinite(Date.parse(v))&&new Date(v).toISOString().slice(0,19)===v.slice(0,19);
const decimal=(v:unknown)=>typeof v==="string"&&v.length<=100&&/^-?\d+(?:\.\d+)?(?:E[+-]?\d+)?$/i.test(v)&&Number.isFinite(Number(v))&&Math.abs(Number(v))<=1e12;
const micros=(v:string)=>BigInt(Date.parse(v.slice(0,19)+"Z"))*BigInt(1000)+BigInt((v.match(/\.(\d+)\+/)?.[1]??"").padEnd(6,"0"));
const secondsBetween=(a:string,b:string)=>{const delta=micros(a)-micros(b);return Number(delta<BigInt(0)?-delta:delta)/1e6;};
const count=(v:unknown)=>Number.isSafeInteger(v)&&Number(v)>=0;
const near=(a:unknown,b:unknown,tolerance=.01)=>decimal(a)&&Number.isFinite(Number(b))&&Math.abs(Number(a)-Number(b))<=tolerance;
const label=(v:unknown)=>typeof v==="string"&&v.length>0&&v.length<=200;
function strings(v:unknown,limit=100):asserts v is string[] {check(Array.isArray(v)&&v.length<=limit&&v.every(label));}
function point(p:RunPoint|null,minute:number,end:number,mode:"paper"|"replay") {
 if(p===null)return;
 exact(p,["at","ledgerId","equity","cash","holdings","issues","strategySha256","strategyEligible"]);
 check(date(p.at)&&Math.floor(Date.parse(p.at)/60000)*60000===minute&&Date.parse(p.at)<end&&count(p.ledgerId));
 check(decimal(p.cash)&&Number(p.cash)>=0&&typeof p.strategyEligible==="boolean"&&(p.strategySha256===null||hash(p.strategySha256)));strings(p.issues);
 check(Array.isArray(p.holdings)&&p.holdings.length<=50);
 const keys=new Set<string>();let equity=Number(p.cash);
 for(const h of p.holdings){exact(h,["key","quantity","average","mark"]);check(label(h.key)&&/^INDIA:(EQUITY|ETF):/.test(h.key)&&!keys.has(h.key)&&count(h.quantity)&&h.quantity>0&&decimal(h.average)&&Number(h.average)>0&&decimal(h.mark)&&Number(h.mark)>0);keys.add(h.key);equity+=h.quantity*Number(h.mark);}
 if(p.equity===null)check(p.issues.length>0&&p.holdings.length===0);else check(p.issues.length===0&&decimal(p.equity)&&Number(p.equity)>0&&near(p.equity,equity));
 if(mode==="replay")check(p.strategySha256===null&&p.strategyEligible===false);
}
export function parseRunComparison(raw:string,tenant:string,now=Date.now()):{report:RunComparisonReport;sha256:string} {
 check(Buffer.byteLength(raw)<=16_000_000);const envelope=JSON.parse(raw);exact(envelope,["payload","sha256"]);
 check(typeof envelope.payload==="string"&&hash(envelope.sha256)&&createHash("sha256").update(envelope.payload).digest("hex")===envelope.sha256);
 const r=JSON.parse(envelope.payload) as RunComparisonReport;
 exact(r,["schema","tenantId","generatedAt","qualification","automaticPromotion","window","sources","configuration","initialState","status","metrics","curve","fills","fillGroups","limitations"]);
 check(r.schema==="pramana.run_comparison.v1"&&r.tenantId===tenant&&r.qualification==="unqualified_paper_replay_diagnostic"&&r.automaticPromotion===false);
 // Publication time can be later than the 2026 observed calendar.
 check(typeof r.generatedAt==="string"&&Number.isFinite(Date.parse(r.generatedAt))&&/(Z|[+-]\d\d:\d\d)$/.test(r.generatedAt)&&Date.parse(r.generatedAt)<=now+5000);
 exact(r.window,["start","end","maxSkewSeconds","calendarSha256","excludedClosedMinutes"]);
 const w=r.window,start=Date.parse(w.start),end=Date.parse(w.end);
 check(date(w.start)&&date(w.end)&&start%60000===0&&end%60000===0&&start<end&&end<=Date.parse(r.generatedAt)&&end-start<=45*86400000&&count(w.maxSkewSeconds)&&w.maxSkewSeconds<=59&&hash(w.calendarSha256));
 const expected:number[]=[];let excluded=0;for(let at=start;at<end;at+=60000){if(regularMinute(at))expected.push(at);else excluded++;}
 check(expected.length>0&&expected.length<=10000&&w.excludedClosedMinutes===excluded&&Array.isArray(r.curve)&&r.curve.length===expected.length);
 const heads={paper:0,replay:0};
 r.curve.forEach((p,i)=>{
  exact(p,["minute","paper","replay","skewSeconds","issues","equityDifference"]);const at=expected[i];check(date(p.minute)&&Date.parse(p.minute)===at);strings(p.issues);
  for(const mode of ["paper","replay"] as const){point(p[mode],at,end,mode);if(p[mode]){check(p[mode]!.ledgerId>=heads[mode]);heads[mode]=p[mode]!.ledgerId;}}
  const issues:string[]=[];
  if(!p.paper)issues.push("paper_observation_missing");else if(p.paper.equity===null)issues.push("paper_valuation_invalid");
  if(!p.replay)issues.push("replay_observation_missing");else if(p.replay.equity===null)issues.push("replay_valuation_invalid");
  const skew=p.paper&&p.replay?secondsBetween(p.paper.at,p.replay.at):null;
  check(p.skewSeconds===skew);if(skew!==null&&skew>w.maxSkewSeconds)issues.push("observation_time_mismatch");
  check(JSON.stringify(p.issues)===JSON.stringify(issues));if(issues.length)check(p.equityDifference===null);else check(near(p.equityDifference,Number(p.paper!.equity)-Number(p.replay!.equity)));
 });
 check(r.status===(r.curve.some(p=>p.issues.length)?"incomplete_observations":"complete_observations"));
 const derived=metrics(r.curve);
 if(derived===null)check(r.metrics===null);else {exact(r.metrics,Object.keys(derived));for(const [k,v] of Object.entries(derived))check(near(r.metrics![k as keyof typeof derived],v,k.includes("Return")||k==="returnDifference"?1e-9:.01));}
 exact(r.sources,["paper","replay"]);for(const [mode,s] of Object.entries(r.sources)){
  const selection=s.observationSelection;
  exact(s,["tenant","startingCapital","sourceSha256",...(selection!==undefined?["observationSelection"]:[])]);
  check(label(s.tenant)&&decimal(s.startingCapital)&&Number(s.startingCapital)>0&&hash(s.sourceSha256));
  if(selection!==undefined){
   check(mode==="paper");exact(selection,["start","end","calendarSha256","expectedMinutes","selectedObservations","totalAccountObservations","excludedObservations"]);
   check(selection.start===w.start&&selection.end===w.end&&selection.calendarSha256===w.calendarSha256&&selection.expectedMinutes===expected.length);
   check(count(selection.selectedObservations)&&count(selection.totalAccountObservations)&&count(selection.excludedObservations)&&selection.selectedObservations===r.curve.filter(p=>p.paper!==null).length&&selection.totalAccountObservations===selection.selectedObservations+selection.excludedObservations);
  }
 }check(r.sources.paper.tenant===tenant);
 const first=r.curve[0];let initial="unavailable";
 if(first.paper&&first.replay&&!first.issues.length){const positions=(p:RunPoint)=>p.holdings.map(h=>[h.key,h.quantity,h.average]).sort((a,b)=>String(a[0])<String(b[0])?-1:1);initial=r.sources.paper.startingCapital===r.sources.replay.startingCapital&&near(first.paper.cash,first.replay.cash)&&JSON.stringify(positions(first.paper))===JSON.stringify(positions(first.replay))?"same_recorded_book":"different_recorded_book";}check(r.initialState===initial);
 const c=r.configuration;exact(c,["observedManifestHashes","observedUnqualifiedPoints","replayRunId","replayDatasetSha256","sourceComparison","differentComponents","strategyEquivalence","replayProtectionModel"]);
 strings(c.observedManifestHashes);check(c.observedManifestHashes.every(hash)&&JSON.stringify(c.observedManifestHashes)===JSON.stringify([...new Set(r.curve.flatMap(p=>p.paper?.strategySha256?[p.paper.strategySha256]:[]))].sort()));
 check(c.observedUnqualifiedPoints===r.curve.filter(p=>p.paper&&!p.paper.strategyEligible).length&&/^[a-f0-9]{32}$/.test(c.replayRunId)&&hash(c.replayDatasetSha256)&&c.strategyEquivalence==="unverified"&&["unavailable","same_inventory","different_inventory"].includes(c.sourceComparison));strings(c.differentComponents);check(["not_simulated","lower_timeframe_ohlc_stop_first"].includes(c.replayProtectionModel));strings(r.limitations,30);
 exact(r.fills,["paper","replay"]);
 for(const mode of ["paper","replay"] as const){check(Array.isArray(r.fills[mode])&&r.fills[mode].length<=20000);const ids=new Set();let last=-Infinity;
  for(const f of r.fills[mode]){exact(f,["ledgerId","orderId","key","symbol","side","quantity","price","cashFees","at"]);check(count(f.ledgerId)&&f.ledgerId>0&&label(f.orderId)&&!ids.has(f.orderId)&&label(f.symbol)&&/^INDIA:(EQUITY|ETF):/.test(f.key)&&f.key.endsWith(":"+f.symbol)&&["BUY","SELL"].includes(f.side)&&count(f.quantity)&&f.quantity>0&&decimal(f.price)&&Number(f.price)>0&&decimal(f.cashFees)&&Number(f.cashFees)>=0&&date(f.at));const at=Date.parse(f.at);check(at>=start&&at<end&&at>=last);last=at;ids.add(f.orderId);}
 }
 const groups=groupFills(r.fills);check(Array.isArray(r.fillGroups)&&r.fillGroups.length===groups.length);
 groups.forEach((g,i)=>{const actual=r.fillGroups[i];exact(actual,["minute","key","symbol","side","paper","replay","quantityDifference","priceDifference"]);for(const k of ["minute","key","symbol","side","quantityDifference"] as const)check(actual[k]===g[k]);check(actual.priceDifference===null?g.priceDifference===null:g.priceDifference!==null&&near(actual.priceDifference,g.priceDifference));for(const mode of ["paper","replay"] as const){const a=actual[mode],e=g[mode];if(e===null)check(a===null);else{exact(a,["quantity","notional","cashFees","fillCount","averagePrice"]);check(a!.quantity===e.quantity&&a!.fillCount===e.fillCount);for(const k of ["notional","cashFees","averagePrice"] as const)check(near(a![k],e[k]));}}});
 return {report:r,sha256:envelope.sha256};
}
export function readRunComparison(expectedSha?:string):RunComparisonState {
 const file=process.env.PRAMANA_RUN_COMPARISON;
 if(!file)return {status:"unavailable",detail:"No paired paper/replay report is configured. Select both source accounts, a retained replay run and an explicit time window.",report:null};
 try {const fd=fs.openSync(/* turbopackIgnore: true */ file,"r");let raw:string;
  try{check(fs.fstatSync(fd).isFile()&&fs.fstatSync(fd).size<=16_000_000);const bytes=Buffer.alloc(16_000_001);let n=0,k=0;do{k=fs.readSync(fd,bytes,n,bytes.length-n,null);n+=k;}while(k&&n<bytes.length);check(n<=16_000_000);raw=bytes.subarray(0,n).toString("utf8");}finally{fs.closeSync(fd);}
  const parsed=parseRunComparison(raw,tenantId);if(expectedSha!==undefined)check(hash(expectedSha)&&expectedSha===parsed.sha256);
  return {status:"published",detail:"Recorded paper observations paired with a retained historical replay. Matching strategy, provider inputs and execution conditions remain unqualified.",...parsed};
 } catch {return {status:"invalid",detail:"The selected run comparison is missing, changed or invalid. No comparison result is available.",report:null};}
}
export function runComparisonContext(expectedSha?:string) {
 const state=readRunComparison(expectedSha);if(expectedSha&&!state.report)throw new Error("Selected run comparison is unavailable or has changed");
 const r=state.report;
 return {status:state.status,detail:state.detail,reportSha256:state.sha256,asOf:r?.window.end,summary:r?{qualification:r.qualification,window:r.window,observationSelection:r.sources.paper.observationSelection,initialState:r.initialState,configuration:{sourceComparison:r.configuration.sourceComparison,differentComponents:r.configuration.differentComponents,strategyEquivalence:r.configuration.strategyEquivalence,replayProtectionModel:r.configuration.replayProtectionModel,observedUnqualifiedPoints:r.configuration.observedUnqualifiedPoints},minutes:r.curve.length,pairedMinutes:r.curve.filter(p=>!p.issues.length).length,issueCodes:[...new Set(r.curve.flatMap(p=>p.issues))],metrics:r.metrics,fillCounts:{paper:r.fills.paper.length,replay:r.fills.replay.length},fillGroups:r.fillGroups.length}:null,limitations:"Historical diagnostic only. No same-strategy/OOS qualification, source authenticity, causal attribution or automatic promotion. Account identities, raw fills and source records are excluded."};
}
