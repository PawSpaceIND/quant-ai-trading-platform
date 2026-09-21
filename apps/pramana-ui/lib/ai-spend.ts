import {DatabaseSync} from "node:sqlite";
import {randomUUID} from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const POLICY = "atlas-text-usd-2026-09-21";
const EXPIRES = "2026-10-21";
const SCHEMA = `
CREATE TABLE IF NOT EXISTS ai_spend_policy(id INTEGER PRIMARY KEY CHECK(id=1), activation_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_spend_days(day TEXT PRIMARY KEY, limit_micro INTEGER NOT NULL, spent_micro INTEGER NOT NULL DEFAULT 0, reserved_micro INTEGER NOT NULL DEFAULT 0, carried_micro INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ai_spend_requests(id TEXT PRIMARY KEY, day TEXT NOT NULL, model TEXT NOT NULL, policy TEXT NOT NULL, reserved_micro INTEGER NOT NULL, spent_micro INTEGER, status TEXT NOT NULL);`;
export type SpendTicket = {database:string; id:string} | null;
export class SpendRefused extends Error {
  constructor(){super("Daily AI dollar budget is exhausted or unavailable. Paid calls are paused; market monitoring and protection continue.");}
}
type Payload = {model:string; max_tokens:number; system:string; messages:{role:string;content:string}[]};
function validDay(value:unknown):value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0,10) === value;
}

export function estimateChat(payload:Payload,day:string){
  if(day>EXPIRES || payload.model!=="claude-sonnet-5" || !Number.isSafeInteger(payload.max_tokens)
    || payload.max_tokens<=0 || payload.max_tokens>16384 || typeof payload.system!=="string"
    || !Array.isArray(payload.messages) || payload.messages.some(m=>!m || typeof m.content!=="string"
      || !["user","assistant"].includes(m.role))
    || Object.keys(payload).some(k=>!["model","max_tokens","system","messages"].includes(k)))throw new SpendRefused();
  const tokens=Buffer.byteLength(JSON.stringify(payload),"utf8")*2+4096;
  if(tokens>250000)throw new SpendRefused();
  return Math.ceil((tokens*40+payload.max_tokens*100)/10);
}

export function reserveChat(payload:Payload,env:Record<string,string|undefined>=process.env,day=new Date().toISOString().slice(0,10)):SpendTicket {
  if(env.PRAMANA_AI_DAILY_USD_LIMIT===undefined)return null;
  let db:DatabaseSync|undefined;
  try {
    if(!validDay(day))throw new SpendRefused();
    const raw=env.PRAMANA_AI_DAILY_USD_LIMIT;
    if(!/^[0-9]{1,6}(\.[0-9]{1,2})?$/.test(raw))throw new SpendRefused();
    const [whole,fraction=""]=raw.split(".");
    const cap=Number(whole)*1000000+Number(fraction.padEnd(2,"0"))*10000;
    if(!Number.isSafeInteger(cap)||cap<=0||cap>1000000000)throw new SpendRefused();
    const amount=estimateChat(payload,day),database=env.PRAMANA_AI_SPEND_DB??"/data/ai-spend.sqlite";
    if(!database||database===":memory:"||!fs.statSync(path.dirname(database)).isDirectory())throw new SpendRefused();
    db=new DatabaseSync(database);
    db.exec("PRAGMA busy_timeout=2000;"+SCHEMA);
    db.exec("BEGIN IMMEDIATE");
    db.prepare("INSERT OR IGNORE INTO ai_spend_policy VALUES(1,?)").run(day);
    const activation=(db.prepare("SELECT activation_day FROM ai_spend_policy WHERE id=1").get() as {activation_day:string}).activation_day;
    if(!validDay(activation))throw new SpendRefused();
    db.prepare("INSERT OR IGNORE INTO ai_spend_days(day,limit_micro,carried_micro) VALUES(?,?,?)").run(day,cap,day<=activation?cap:0);
    db.prepare("UPDATE ai_spend_days SET limit_micro=MIN(limit_micro,?) WHERE day=?").run(cap,day);
    const counters=db.prepare("SELECT limit_micro,spent_micro,reserved_micro,carried_micro FROM ai_spend_days WHERE day=?").get(day);
    if(!counters||Object.values(counters).some(v=>typeof v!=="number"||!Number.isSafeInteger(v)||v<0))throw new SpendRefused();
    const result=db.prepare("UPDATE ai_spend_days SET reserved_micro=reserved_micro+? WHERE day=? AND carried_micro+spent_micro+reserved_micro+?<=limit_micro").run(amount,day,amount);
    if(result.changes!==1){db.exec("COMMIT");throw new SpendRefused();}
    const id=randomUUID().replaceAll("-","");
    db.prepare("INSERT INTO ai_spend_requests VALUES(?,?,?,?,?,NULL,'reserved')").run(id,day,payload.model,POLICY,amount);
    db.exec("COMMIT");return {database,id};
  }catch{throw new SpendRefused();}finally{db?.close();}
}

export function settleChat(ticket:SpendTicket,usage:unknown){
  if(!ticket||!usage||typeof usage!=="object")return;
  const u=usage as Record<string,unknown>;
  function count(k:string,optional=false){const n=u[k]??(optional?0:undefined);
    if(typeof n!=="number"||!Number.isSafeInteger(n)||n<0||n>10000000)throw new Error("usage_invalid");return n;}
  let db:DatabaseSync|undefined;
  try {
    const spent=Math.ceil((count("input_tokens")*20+count("output_tokens")*100
      +count("cache_creation_input_tokens",true)*40+count("cache_read_input_tokens",true)*2)/10);
    db=new DatabaseSync(ticket.database);db.exec("PRAGMA busy_timeout=2000;BEGIN IMMEDIATE");
    const row=db.prepare("SELECT day,model,reserved_micro,status FROM ai_spend_requests WHERE id=?").get(ticket.id) as {day:string;model:string;reserved_micro:number;status:string}|undefined;
    if(!row||row.status!=="reserved"||row.model!=="claude-sonnet-5"){db.exec("ROLLBACK");return;}
    db.prepare("UPDATE ai_spend_requests SET spent_micro=?,status='settled' WHERE id=?").run(spent,ticket.id);
    db.prepare("UPDATE ai_spend_days SET spent_micro=spent_micro+?,reserved_micro=reserved_micro-? WHERE day=?").run(spent,row.reserved_micro,row.day);
    db.exec("COMMIT");
  }catch{/* Unknown completion stays reserved. */}finally{db?.close();}
}

export function spendStatus(env:Record<string,string|undefined>=process.env,day=new Date().toISOString().slice(0,10)): {status:string;remainingUsd:number;day?:string;limitUsd?:number;spentUsd?:number;reservedUsd?:number;reset?:string;invoice?:boolean}|null {
  if(env.PRAMANA_AI_DAILY_USD_LIMIT===undefined)return null;
  let db:DatabaseSync|undefined;
  try {
    const raw=env.PRAMANA_AI_DAILY_USD_LIMIT;
    if(!/^[0-9]{1,6}(\.[0-9]{1,2})?$/.test(raw))throw new SpendRefused();
    const cap=Math.round(Number(raw)*1000000);
    if(cap<=0||cap>1000000000)throw new SpendRefused();
    const database=env.PRAMANA_AI_SPEND_DB??"/data/ai-spend.sqlite";
    const base={day,limitUsd:cap/1000000,reset:"05:30 IST",invoice:false};
    if(!fs.existsSync(database))return {...base,status:"activation_hold",spentUsd:0,reservedUsd:0,remainingUsd:0};
    db=new DatabaseSync(database,{readOnly:true});
    const activation=db.prepare("SELECT activation_day FROM ai_spend_policy WHERE id=1").get() as {activation_day:string}|undefined;
    const row=db.prepare("SELECT limit_micro,spent_micro,reserved_micro,carried_micro FROM ai_spend_days WHERE day=?").get(day);
    if(!activation||!validDay(activation.activation_day)||!validDay(day)||day>EXPIRES)throw new SpendRefused();
    if(row&&Object.values(row).some(v=>typeof v!=="number"||!Number.isSafeInteger(v)||v<0))throw new SpendRefused();
    const limit=Math.min(cap,Number(row?.limit_micro??cap)),spent=Number(row?.spent_micro??0),reserved=Number(row?.reserved_micro??0);
    const held=Number(row?.carried_micro??(day<=activation.activation_day?cap:0));
    const remaining=Math.max(0,limit-spent-reserved-held);
    return {...base,limitUsd:limit/1000000,spentUsd:spent/1000000,reservedUsd:reserved/1000000,remainingUsd:remaining/1000000,
      status:held>0?"activation_hold":remaining>0?"available":"exhausted"};
  }catch{return {status:"unavailable",remainingUsd:0};}finally{db?.close();}
}
