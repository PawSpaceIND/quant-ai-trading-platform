import test from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.mjs";
const auth="Basic "+btoa("test:dummy-password");
function env(row=null){return {VIEW_AUTH:auth,PUBLISH_TOKEN:"test-only-token",ASSETS:{fetch:async()=>new Response("asset")},DB:{prepare:()=>({first:async()=>row,bind(){return this;},run:async()=>({success:true})})}}}
test("all assets and APIs require authentication",async()=>{for(const path of ["/","/_next/file.js","/api/market"]){assert.equal((await worker.fetch(new Request("https://test"+path),env())).status,401);}});
test("viewer cannot upload and publisher cannot view",async()=>{assert.equal((await worker.fetch(new Request("https://test/_ingest",{method:"POST",headers:{Authorization:auth},body:"{}"}),env())).status,401);assert.equal((await worker.fetch(new Request("https://test/api/market",{headers:{Authorization:"Bearer test-only-token"}}),env())).status,401);});
test("missing secrets fail closed",async()=>{
 const missing=env(); delete missing.VIEW_AUTH; delete missing.PUBLISH_TOKEN;
 assert.equal((await worker.fetch(new Request("https://test/",{headers:{Authorization:auth}}),missing)).status,401);
 assert.equal((await worker.fetch(new Request("https://test/_ingest",{method:"POST",headers:{Authorization:"Bearer undefined"},body:"{}"}),missing)).status,401);
});
test("private snapshot round trip and staleness",async()=>{const row={body:JSON.stringify({"/api/market":{rows:[{symbol:"INFY"}]}}),source_at:"2020-01-01T00:00:00Z"};let r=await worker.fetch(new Request("https://test/api/market",{headers:{Authorization:auth}}),env(row));assert.equal((await r.json()).rows[0].symbol,"INFY");assert.equal(r.headers.get("Cache-Control"),"no-store");r=await worker.fetch(new Request("https://test/api/cloud-status",{headers:{Authorization:auth}}),env(row));assert.equal((await r.json()).stale,true);});
test("ingest rejects incomplete and wrong tenant",async()=>{const base={sourceAt:new Date().toISOString(),snapshots:{}};for(const p of ["market","portfolio/mtm","intelligence/swarm","execution/friction","execution/trades"])base.snapshots["/api/"+p]={};const send=body=>worker.fetch(new Request("https://test/_ingest",{method:"POST",headers:{Authorization:"Bearer test-only-token"},body:JSON.stringify(body)}),env());assert.equal((await send({})).status,400);assert.equal((await send(base)).status,400);base.snapshots["/api/portfolio/mtm"].tenantId="india-paper";assert.equal((await send(base)).status,200);});

test("viewer cannot mutate or read unknown API",async()=>{
 assert.equal((await worker.fetch(new Request("https://test/api/market",{method:"POST",headers:{Authorization:auth}}),env())).status,405);
 assert.equal((await worker.fetch(new Request("https://test/api/secrets",{headers:{Authorization:auth}}),env())).status,404);
});
test("failed snapshot is visibly unavailable",async()=>{
 const r=await worker.fetch(new Request("https://test/api/market",{headers:{Authorization:auth}}),env());
 assert.equal(r.status,503);
});

test("research lab and operator notes are excluded before cloud snapshot storage",async()=>{
 const snapshots={};for(const p of ["market","portfolio/mtm","intelligence/swarm","execution/friction","execution/trades"])snapshots["/api/"+p]={};
 snapshots["/api/portfolio/mtm"].tenantId="india-paper";
 snapshots["/api/workspace"]={tenantId:"india-paper",researchLab:{private:"RESEARCH_SENTINEL"},researchPortfolio:{private:"PORTFOLIO_SENTINEL"},paperContribution:{private:"CONTRIBUTION_SENTINEL"},companyEvents:{private:"EVENT_SENTINEL"},audit:["NOTES_SENTINEL"],runtime:{status:"running"}};
 let stored;const target=env();target.DB.prepare=()=>({bind(body){stored=JSON.parse(body);return this;},run:async()=>({success:true})});
 const response=await worker.fetch(new Request("https://test/_ingest",{method:"POST",headers:{Authorization:"Bearer test-only-token"},body:JSON.stringify({sourceAt:new Date().toISOString(),snapshots})}),target);
 assert.equal(response.status,200);assert.equal(stored["/api/workspace"].researchLab,undefined);assert.equal(stored["/api/workspace"].researchPortfolio,undefined);assert.equal(stored["/api/workspace"].paperContribution,undefined);assert.equal(stored["/api/workspace"].companyEvents,undefined);assert.deepEqual(stored["/api/workspace"].audit,[]);
 assert.equal(stored["/api/workspace"].runtime.status,"running");
 assert(!JSON.stringify(stored).includes("SENTINEL"));
});

test("shared workspace keeps hosted marks and controls read-only",async()=>{
 const sourceAt=new Date().toISOString();const body={"/api/portfolio/mtm":{tenantId:"india-paper",totalEquity:100000,holdings:[{symbol:"INFY",fresh:true,markSource:"live_tick"}]},"/api/market":{rows:[],fetchedAt:sourceAt},"/api/intelligence/swarm":{agents:[]},"/api/workspace":{tenantId:"india-paper",runtime:{status:"running"},copilotConfigured:true}};
 const r=await worker.fetch(new Request("https://test/api/workspace",{headers:{Authorization:auth}}),env({body:JSON.stringify(body),source_at:sourceAt}));
 const data=await r.json();assert.equal(data.runtime.status,"snapshot");assert.equal(data.portfolio.holdings[0].fresh,false);assert.equal(data.copilotConfigured,false);assert.equal(data.liveEnabled,false);
 for(const route of ["/api/control","/api/copilot","/api/watchlist"]){assert.equal((await worker.fetch(new Request("https://test"+route,{method:"POST",headers:{Authorization:auth},body:"{}"}),env())).status,405);}
});

function healthyRow() {
 const at=new Date().toISOString();
 return {source_at:at,received_at:at,body:JSON.stringify({"/api/workspace":{
  tenantId:"india-paper",runtime:{status:"running",mode:"paper",halted:false,updatedAt:at}
 }})};
}
function monitorEnv(row=healthyRow()) {return {...env(row),MONITOR_TOKEN:"dummy-monitor-only"};}
function probe(headers={Authorization:"Bearer dummy-monitor-only"},method="GET") {
 return new Request("https://test/healthz",{headers,method});
}
test("monitor credential is separate and cannot access dashboard or ingest",async()=>{
 for(const authorization of [auth,"Bearer test-only-token",""]) {
  assert.equal((await worker.fetch(probe({Authorization:authorization}),monitorEnv())).status,401);
 }
 assert.equal((await worker.fetch(probe(),env(healthyRow()))).status,401);
 for(const path of ["/api/workspace","/_ingest"]) {
  assert.equal((await worker.fetch(new Request("https://test"+path,{method:path==="/_ingest"?"POST":"GET",headers:{Authorization:"Bearer dummy-monitor-only"}}),monitorEnv())).status,401);
 }
});
test("monitor returns bounded evidence and never portfolio or operator notes",async()=>{
 const result=await worker.fetch(probe(),monitorEnv());
 assert.equal(result.status,200);assert.equal(result.headers.get("Cache-Control"),"no-store");
 const body=await result.json();assert.equal(body.status,"observation_ok");assert.deepEqual(body.reasons,[]);
 assert.equal(body.runtime,undefined);assert.equal(body.portfolio,undefined);
 assert.equal((await worker.fetch(probe({},"POST"),monitorEnv())).status,401);
 assert.equal((await worker.fetch(probe(undefined,"POST"),monitorEnv())).status,405);
 const head=await worker.fetch(probe(undefined,"HEAD"),monitorEnv());assert.equal(head.status,200);assert.equal(await head.text(),"");
});
test("fresh publication cannot hide stale engine heartbeat",async()=>{
 const row=healthyRow();const body=JSON.parse(row.body);
 body["/api/workspace"].runtime.updatedAt=new Date(Date.now()-151000).toISOString();row.body=JSON.stringify(body);
 const result=await worker.fetch(probe(),monitorEnv(row));assert.equal(result.status,503);
 assert.ok((await result.json()).reasons.includes("engine_heartbeat_stale_or_invalid"));
});
test("monitor fails closed on absent invalid future halted or mismatched evidence",async()=>{
 const rows=[null,{...healthyRow(),body:"invalid"}];
 for(const field of ["source_at","received_at"])for(const value of ["invalid",new Date(Date.now()-181000).toISOString(),new Date(Date.now()+10000).toISOString()])rows.push({...healthyRow(),[field]:value});
 for(const change of [{halted:true},{halted:undefined},{status:"snapshot"},{mode:"live"},{updatedAt:new Date(Date.now()+10000).toISOString()}]){
  const row=healthyRow(),body=JSON.parse(row.body);Object.assign(body["/api/workspace"].runtime,change);row.body=JSON.stringify(body);rows.push(row);
 }
 for(const row of rows)assert.equal((await worker.fetch(probe(),monitorEnv(row))).status,503);
 const broken=monitorEnv();broken.DB.prepare=()=>{throw new Error("private database error")};
 const result=await worker.fetch(probe(),broken);assert.equal(result.status,503);assert.deepEqual((await result.json()).reasons,["monitor_storage_unavailable"]);
});
