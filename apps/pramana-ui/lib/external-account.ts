import fs from "node:fs";
import {createHash} from "node:crypto";
import {canonical} from "./broker-capture";
import {tenantId} from "./db";

export type ExternalAccountReport={schema:"pramana.external_account_snapshot.v1";scope:string;broker:"zerodha-kite";tenantId:string;accountRef:string;startedAt:string;finishedAt:string;status:"consistent"|"changing"|"incomplete";fundsBefore:Record<string,unknown>;positionsBefore:Record<string,unknown>[];funds:Record<string,unknown>;positions:Record<string,unknown>[];limitations:string[];sha256:string};
export type ExternalAccountState={status:"unavailable"|"invalid"|"stale"|"available";detail:string;report:ExternalAccountReport|null};
function check(value:unknown):asserts value {if(!value)throw Error("invalid_external_account_snapshot");}
function exact(value:unknown,keys:string[]):asserts value is Record<string,unknown>{check(value&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key)));}
const amount=(v:unknown)=>v===null||typeof v==="string"&&v.length<=80&&/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d{1,2})?$/.test(v)&&Number.isFinite(Number(v))&&Math.abs(Number(v))<=1e12;
const fundKeys=["broker","currency","cash_balance","available_balance","net_liquidation","net_trading_funds","opening_balance","segment"];
const positionKeys=["broker","instrument_id","symbol","exchange","currency","security_type","product","quantity","average_cost","scope","cost_basis","model"];
function funds(v:unknown):asserts v is Record<string,unknown>{
 exact(v,fundKeys);check(v.broker==="zerodha-kite"&&v.currency==="INR"&&v.segment==="equity"&&v.net_liquidation===null);
 for(const k of ["cash_balance","available_balance","net_trading_funds","opening_balance"])check(amount(v[k]));
}
function positions(v:unknown):asserts v is Record<string,unknown>[] {
 check(Array.isArray(v)&&v.length<=10000);const seen=new Set<string>();
 for(const item of v){exact(item,positionKeys);check(item.broker==="zerodha-kite"&&item.currency===null&&item.security_type===null&&item.model===null&&item.cost_basis==="per_unit"&&item.scope==="broker_net_positions_excludes_separate_holdings");
  for(const k of ["instrument_id","symbol","exchange","product"])check(typeof item[k]==="string"&&item[k].length>0&&item[k].length<=160&&!/[\x00-\x1f]/.test(item[k]));
  check(typeof item.quantity==="string"&&amount(item.quantity)&&Number(item.quantity)>0&&Number.isSafeInteger(Number(item.quantity))&&typeof item.average_cost==="string"&&amount(item.average_cost)&&Number(item.average_cost)>=0);
  const id=JSON.stringify([item.instrument_id,item.exchange,item.product]);check(!seen.has(id));seen.add(id);
 }
}
const time=(v:unknown)=>typeof v==="string"&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00$/.test(v)&&Number.isFinite(Date.parse(v));
export function parseExternalAccount(raw:string,tenant:string,ref:string):ExternalAccountReport {
 check(Buffer.byteLength(raw)<=8_000_000);const r=JSON.parse(raw);
 exact(r,["schema","scope","broker","tenantId","accountRef","startedAt","finishedAt","status","fundsBefore","positionsBefore","funds","positions","limitations","sha256"]);
 check(r.schema==="pramana.external_account_snapshot.v1"&&r.scope==="selected_external_kite_equity_net_positions_and_funds_excludes_depository_holdings"&&r.broker==="zerodha-kite"&&r.tenantId===tenant&&r.accountRef===ref&&/^[a-f0-9]{64}$/.test(ref));
 check(time(r.startedAt)&&time(r.finishedAt));const start=Date.parse(String(r.startedAt)),end=Date.parse(String(r.finishedAt));
 check(end>=start&&end-start<=30000&&new Date(start+330*60000).toISOString().slice(0,10)===new Date(end+330*60000).toISOString().slice(0,10));
 funds(r.fundsBefore);funds(r.funds);positions(r.positionsBefore);positions(r.positions);
 const stable=canonical(r.fundsBefore)===canonical(r.funds)&&canonical(r.positionsBefore)===canonical(r.positions);
 check(r.status===(stable?(r.funds.cash_balance!==null&&r.funds.available_balance!==null?"consistent":"incomplete"):"changing"));
 check(Array.isArray(r.limitations)&&r.limitations.length===3&&r.limitations.every((v:unknown)=>typeof v==="string"&&v.length<=300));
 const {sha256,...unsigned}=r;check(typeof sha256==="string"&&createHash("sha256").update(canonical(unsigned)).digest("hex")===sha256);
 return r as ExternalAccountReport;
}
export function readExternalAccount(now=Date.now()):ExternalAccountState {
 const file=process.env.PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT,ref=process.env.PRAMANA_EXTERNAL_ACCOUNT_REF;
 if(!file||!ref)return {status:"unavailable",detail:"No selected external funds/net-position snapshot is configured.",report:null};
 try{const fd=fs.openSync(file,"r");let raw:string;try{const stat=fs.fstatSync(fd);check(stat.isFile()&&stat.size<=8_000_000);raw=fs.readFileSync(fd,"utf8");}finally{fs.closeSync(fd);}
  const report=parseExternalAccount(raw,tenantId,ref),age=now-Date.parse(report.finishedAt);check(age>=-5000);
  return {status:age>120000?"stale":"available",detail:age>120000?"Historical selected-account snapshot; current broker state is unverified.":"Repeated selected-account reads only. Depository holdings and paper-account reconciliation remain unverified.",report};
 }catch{return {status:"invalid",detail:"Selected external account snapshot is invalid, unreadable or belongs to another account.",report:null};}
}
export function externalAccountContext(){const state=readExternalAccount();return {status:state.status,detail:state.detail,captureStatus:state.report?.status,asOf:state.report?.finishedAt,netPositionCount:state.report?.positions.length,
 omissions:"Account reference, instrument identities, cash and position quantities are excluded. Depository holdings, settlement and paper-account parity are not established."};}
