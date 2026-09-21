import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {spawnSync} from "node:child_process";
import {DatabaseSync} from "node:sqlite";
import {reserveChat,settleChat,estimateChat,spendStatus,SpendRefused} from "../lib/ai-spend";
import {requestClaude} from "../lib/claude-response";
const payload={model:"claude-sonnet-5",max_tokens:1400,system:"test",messages:[{role:"user" as const,content:"test"}]};

test("activation closes unknown prior spending; restart and settlement retain a shared cap",()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-usd-"));
  const env={PRAMANA_AI_DAILY_USD_LIMIT:"2.50",PRAMANA_AI_SPEND_DB:path.join(dir,"spend.db")};
  try{
    assert.equal(spendStatus(env,"2026-09-21")?.status,"activation_hold");
    assert.throws(()=>reserveChat(payload,env,"2026-09-21"),SpendRefused);
    assert.equal(spendStatus(env,"2026-09-21")?.remainingUsd,0);
    const t=reserveChat(payload,env,"2026-09-22");assert.ok(t);
    settleChat(t,{input_tokens:1000,output_tokens:100});
    settleChat(t,{input_tokens:1000,output_tokens:100});
    const s=spendStatus(env,"2026-09-22");assert.equal(s?.spentUsd,0.003);assert.equal(s?.reservedUsd,0);
    const u=reserveChat(payload,env,"2026-09-22");settleChat(u,{input_tokens:100});
    assert.equal(spendStatus(env,"2026-09-22")?.reservedUsd,estimateChat(payload,"2026-09-22")/1000000);
    assert.throws(()=>reserveChat(payload,{...env,PRAMANA_AI_DAILY_USD_LIMIT:"0.01"},"2026-09-22"),SpendRefused);
    assert.throws(()=>reserveChat(payload,env,"2026-09-22"),SpendRefused);
    assert.equal(spendStatus(env,"2026-09-22")?.limitUsd,0.01);
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test("Python engine and Node chat debit the same SQLite dollar allowance",()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-usd-cross-"));
  const env={PRAMANA_AI_DAILY_USD_LIMIT:"2.50",PRAMANA_AI_SPEND_DB:path.join(dir,"spend.db")};
  try{
    assert.throws(()=>reserveChat(payload,env,"2026-09-21"),SpendRefused);
    const py=`import json,sys\nimport importlib.util,os\nspec=importlib.util.spec_from_file_location("spend",os.path.join(os.environ["PYTHONPATH"],"quant_ai/llm/spend.py"))\nm=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\nreserve,settle,estimate=m.reserve,m.settle,m.estimate\nx=json.load(sys.stdin)\nt=reserve(x['payload'],x['env'],day='2026-09-22')\nsettle(t,{'input_tokens':1000,'output_tokens':100})\nprint(json.dumps({'estimate':estimate(x['payload'],'2026-09-22')}))`;
    const run=spawnSync("python3",["-c",py],{input:JSON.stringify({payload,env}),encoding:"utf8",env:{...process.env,PYTHONPATH:path.resolve("../../src")}});
    assert.equal(run.status,0,run.stderr);
    assert.equal(JSON.parse(run.stdout).estimate,estimateChat(payload,"2026-09-22"));
    assert.equal(spendStatus(env,"2026-09-22")?.spentUsd,0.003);
    const ticket=reserveChat(payload,env,"2026-09-22");assert.ok(ticket);
    settleChat(ticket,{input_tokens:1000,output_tokens:100});
    assert.equal(spendStatus(env,"2026-09-22")?.spentUsd,0.006);
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});

test("closed budget refuses chat transport and every retry needs a new reservation",async(t)=>{
  t.mock.timers.enable({apis:["Date"],now:new Date("2026-09-22T12:00:00Z")});
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),"atlas-usd-io-"));
  const oldLimit=process.env.PRAMANA_AI_DAILY_USD_LIMIT,oldDb=process.env.PRAMANA_AI_SPEND_DB;
  process.env.PRAMANA_AI_DAILY_USD_LIMIT="2.50";process.env.PRAMANA_AI_SPEND_DB=path.join(dir,"spend.db");
  let calls=0;
  const transport=(async()=>{calls++;return new Response(JSON.stringify({stop_reason:"max_tokens",content:[],usage:{input_tokens:200,output_tokens:1400}}),{status:200});}) as typeof fetch;
  try{
    const first=await requestClaude(payload,"synthetic",transport,()=>true);
    assert.match(first.error??"",/dollar budget/);assert.equal(calls,0);
    // Seed a fresh admitted day in this disposable test database, with only enough
    // for the first call. The retry's larger maximum must be refused before I/O.
    const db=new DatabaseSync(process.env.PRAMANA_AI_SPEND_DB);
    db.prepare("UPDATE ai_spend_policy SET activation_day=?").run("2026-01-01");
    db.prepare("UPDATE ai_spend_days SET carried_micro=0,limit_micro=?").run(estimateChat(payload,new Date().toISOString().slice(0,10)));
    db.close();
    const second=await requestClaude(payload,"synthetic",transport,()=>true);
    assert.equal(calls,1);assert.match(second.error??"",/dollar budget/);
  }finally{
    if(oldLimit===undefined)delete process.env.PRAMANA_AI_DAILY_USD_LIMIT;else process.env.PRAMANA_AI_DAILY_USD_LIMIT=oldLimit;
    if(oldDb===undefined)delete process.env.PRAMANA_AI_SPEND_DB;else process.env.PRAMANA_AI_SPEND_DB=oldDb;
    fs.rmSync(dir,{recursive:true,force:true});
  }
});

test("unknown models, expired pricing and hosted tools cannot obtain a cost reservation",()=>{
  assert.throws(()=>estimateChat({...payload,model:"unknown"},"2026-09-22"),SpendRefused);
  assert.throws(()=>estimateChat(payload,"2026-10-22"),SpendRefused);
  assert.throws(()=>estimateChat({...payload,max_tokens:NaN},"2026-09-22"),SpendRefused);
  assert.throws(()=>estimateChat({...payload,tools:[{type:"web_search"}]} as typeof payload,"2026-09-22"),SpendRefused);
});
