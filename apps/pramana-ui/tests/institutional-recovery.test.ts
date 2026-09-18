import test,{type TestContext} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {randomBytes} from "node:crypto";
import {NextRequest} from "next/server";
import {GET,POST} from "../app/api/institutional-recovery/route";
import {makeSession,SESSION_COOKIE} from "../lib/auth";
import {applyRecovery,inspectRecovery,readRecoveryOutcome} from "../lib/institutional-recovery-client";
import {RECOVERY_CONFIRMATION} from "../lib/institutional-recovery-contract";
const program="programme-1",requestId="recovery-1",context="a".repeat(64);
const preview=()=>({tenant_id:"tenant",program_id:program,context_sha256:context,program_state:"ACTIVE",
  slice_states:["DISPATCHING"],source_revision_sha256:"b".repeat(64),execution_authorized:false,
  confirmation_required:RECOVERY_CONFIRMATION,scope:"private backend explanation"});
const outcome=()=>({tenant_id:"tenant",request_id:requestId,program_id:program,actor_key_id:"actor-1",
  context_sha256:context,status:"RETURNED",recorded_at:"2026-09-18T06:00:00+00:00",replayed:false,execution_authorized:false,
  result:{program_id:program,tenant_id:"tenant",program_state:"COMPLETE",recovery_stage:"COMPLETE",committed_order_ids:["PAPER-1"],
    recovered_sequences:[1],source_revision_sha256:"b".repeat(64),reason_code:"recorded_complete",execution_authorized:false}});
const body=()=>({program_id:program,request_id:requestId,expected_context_sha256:context,confirmation:RECOVERY_CONFIRMATION});
function setup(t:TestContext) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),"pramana-recovery-gateway-"));
  const read=path.join(root,"read.key"),apply=path.join(root,"apply.key");
  const token=randomBytes(32).toString("base64url");fs.writeFileSync(read,token+"\n",{mode:0o600});fs.writeFileSync(apply,token,{mode:0o600});
  const env={PRAMANA_OPERATOR_API_ORIGIN:"http://127.0.0.1:8765",PRAMANA_OPERATOR_TENANT:"tenant",
    PRAMANA_OPERATOR_READ_KEY_FILE:read,PRAMANA_OPERATOR_APPLY_KEY_FILE:apply,
    PRAMANA_DASHBOARD_SECRET:"synthetic-unit-dashboard-secret-at-least-32-characters",PRAMANA_PUBLIC_ORIGIN:"http://localhost"};
  const before=Object.fromEntries(Object.keys(env).map(k=>[k,process.env[k]]));Object.assign(process.env,env);
  t.after(()=>{for(const [k,v] of Object.entries(before)){if(v===undefined)delete process.env[k];else process.env[k]=v;}fs.rmSync(root,{recursive:true,force:true});});
  return {root,read,apply,token};
}
function req(method="GET",query="",payload?:unknown,session=true,origin="http://localhost") {
  return new NextRequest("http://localhost/api/institutional-recovery"+query,{method,
    headers:{...(session?{cookie:`${SESSION_COOKIE}=${makeSession()}`} :{}),origin,"content-type":"application/json"},
    ...(payload===undefined?{}:{body:JSON.stringify(payload)})});
}
for(const method of ["GET","POST"]) test(`session is required before storage or HTTP: ${method}`,async t=>{
  setup(t);const fetch=t.mock.method(globalThis,"fetch",async()=>{throw new Error("must not fetch");});
  const file=t.mock.method(fs,"lstatSync",()=>{throw new Error("must not read");});
  const result=method==="GET"?await GET(req("GET","?program_id="+program,undefined,false)):await POST(req("POST","",body(),false));
  assert.equal(result.status,401);assert.equal(result.headers.get("Cache-Control"),"no-store");
  assert.equal(fetch.mock.callCount(),0);assert.equal(file.mock.callCount(),0);
});
for(const origin of ["https://attacker.invalid",""]) test(`write origin refused: ${origin||"missing"}`,async t=>{
  setup(t);const spy=t.mock.method(globalThis,"fetch",async()=>new Response());
  assert.equal((await POST(req("POST","",body(),true,origin))).status,403);assert.equal(spy.mock.callCount(),0);
});
for(const query of ["","?tenant=other","?program_id=x&program_id=y","?program_id=x&database=bad","?request_id=../outside"]) test(`query override refused: ${query}`,async t=>{
  setup(t);const spy=t.mock.method(globalThis,"fetch",async()=>new Response());
  assert.equal((await GET(req("GET",query))).status,400);assert.equal(spy.mock.callCount(),0);
});
for(const field of ["tenant_id","database","url","action","key","order"]) test(`write body cannot select ${field}`,async t=>{
  setup(t);const spy=t.mock.method(globalThis,"fetch",async()=>new Response());
  assert.equal((await POST(req("POST","",{...body(),[field]:"arbitrary"}))).status,400);assert.equal(spy.mock.callCount(),0);
});
for(const origin of ["http://remote.invalid","http://localhost:8765","https://user:pass@host.invalid","https://host.invalid/path","https://host.invalid?x=1","file:///tmp/x"]) test(`unsafe configured origin refuses: ${origin}`,async t=>{
  setup(t);process.env.PRAMANA_OPERATOR_API_ORIGIN=origin;const spy=t.mock.method(globalThis,"fetch",async()=>new Response());
  await assert.rejects(()=>inspectRecovery(program),/recovery_not_configured/);assert.equal(spy.mock.callCount(),0);
});
for(const fault of ["missing","symlink","hardlink","public","malformed","oversize"]) test(`credential file refusal: ${fault}`,async t=>{
  const f=setup(t);
  if(fault==="missing")fs.unlinkSync(f.read);
  else if(fault==="symlink"){fs.unlinkSync(f.read);fs.symlinkSync(f.apply,f.read);}
  else if(fault==="hardlink"){fs.unlinkSync(f.read);fs.linkSync(f.apply,f.read);}
  else if(fault==="public")fs.chmodSync(f.read,0o644);
  else fs.writeFileSync(f.read,fault==="malformed"?"bad":"x".repeat(1000));
  const spy=t.mock.method(globalThis,"fetch",async()=>new Response());
  await assert.rejects(()=>inspectRecovery(program),/recovery_credential_unavailable/);assert.equal(spy.mock.callCount(),0);
});
test("read-only preview is bounded, account-bound and contains no credentials",async t=>{
  const f=setup(t);delete process.env.PRAMANA_OPERATOR_APPLY_KEY_FILE;
  t.mock.method(globalThis,"fetch",async(input:RequestInfo|URL,options?:RequestInit)=>{
    assert.equal(input,"http://127.0.0.1:8765/v1/institutional/programs/programme-1/recovery");
    assert.equal((options?.headers as Record<string,string>)["X-API-Key"],f.token);
    assert.equal(options?.redirect,"error");assert.equal(options?.cache,"no-store");
    return Response.json({...preview(),raw_key:f.token,private_path:f.root});});
  const result=await inspectRecovery(program);assert.equal(result.canApply,false);assert.equal(result.tenant_id,"tenant");
  assert.ok(!JSON.stringify(result).includes(f.token));assert.ok(!JSON.stringify(result).includes(f.root));
  await assert.rejects(()=>applyRecovery(body()),/recovery_credential_unavailable/);
});
test("apply verifies the saved account and context before one non-retried POST",async t=>{
  setup(t);const calls:string[]=[];
  t.mock.method(globalThis,"fetch",async(input:RequestInfo|URL,options?:RequestInit)=>{
    calls.push(options?.method||"GET");
    if(options?.method==="POST") {assert.deepEqual(JSON.parse(String(options.body)),{request_id:requestId,expected_context_sha256:context,confirmation:RECOVERY_CONFIRMATION});return Response.json(outcome());}
    return Response.json(preview());});
  const r=await applyRecovery(body());assert.equal(r.result?.program_state,"COMPLETE");assert.equal(r.execution_authorized,false);assert.deepEqual(calls,["GET","POST"]);
});
for(const fault of ["tenant","program","context","authority"]) test(`changed preview blocks write: ${fault}`,async t=>{
  setup(t);const calls:string[]=[];
  t.mock.method(globalThis,"fetch",async(_:RequestInfo|URL,options?:RequestInit)=>{calls.push(options?.method||"GET");const v=preview();
    if(fault==="tenant")v.tenant_id="other";if(fault==="program")v.program_id="other";if(fault==="context")v.context_sha256="c".repeat(64);if(fault==="authority")v.execution_authorized=true;
    return Response.json(v);});
  await assert.rejects(()=>applyRecovery(body()));assert.deepEqual(calls,["GET"]);
});
for(const fault of ["network","status","json","oversize","tenant","request","authority"]) test(`post outcome stays unknown without automatic retry: ${fault}`,async t=>{
  const f=setup(t);let posts=0;
  t.mock.method(globalThis,"fetch",async(_:RequestInfo|URL,options?:RequestInit)=>{
    if(options?.method!=="POST")return Response.json(preview());posts++;
    if(fault==="network")throw new Error(f.token+f.root);
    if(fault==="status")return new Response(f.token,{status:500});
    if(fault==="json")return new Response("not json");
    if(fault==="oversize")return new Response("x".repeat(20000));
    const v=outcome();if(fault==="tenant")v.result.tenant_id="other";if(fault==="request")v.request_id="other";if(fault==="authority")v.execution_authorized=true;
    return Response.json(v);});
  const r=await POST(req("POST","",body()));assert.equal(r.status,502);assert.deepEqual(await r.json(),{error:"recovery_outcome_unknown"});assert.equal(posts,1);
});
test("checking a retained request uses GET only and does not reinterpret unknown status",async t=>{
  setup(t);const spy=t.mock.method(globalThis,"fetch",async(_:RequestInfo|URL,options?:RequestInit)=>{assert.equal(options?.method,"GET");return Response.json({...outcome(),status:"REQUESTED",result:null});});
  const r=await readRecoveryOutcome(requestId);assert.equal(r.status,"REQUESTED");assert.equal(r.result,null);assert.equal(spy.mock.callCount(),1);
});

for(const change of ["tenant","origin","credential"]) test(`configuration changed while preview waits cannot redirect a write: ${change}`,async t=>{
  const f=setup(t);let posts=0;
  t.mock.method(globalThis,"fetch",async(_:RequestInfo|URL,options?:RequestInit)=>{
    if(options?.method==="POST"){posts++;return Response.json(outcome());}
    if(change==="tenant")process.env.PRAMANA_OPERATOR_TENANT="other";
    else if(change==="origin")process.env.PRAMANA_OPERATOR_API_ORIGIN="https://new.invalid";
    else fs.writeFileSync(f.apply,randomBytes(32).toString("base64url"));
    return Response.json(preview());});
  await assert.rejects(()=>applyRecovery(body()),/recovery_configuration_changed/);assert.equal(posts,0);
});
