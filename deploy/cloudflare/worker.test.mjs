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

test("shared workspace keeps hosted marks and controls read-only",async()=>{
 const sourceAt=new Date().toISOString();const body={"/api/portfolio/mtm":{tenantId:"india-paper",totalEquity:100000,holdings:[{symbol:"INFY",fresh:true,markSource:"live_tick"}]},"/api/market":{rows:[],fetchedAt:sourceAt},"/api/intelligence/swarm":{agents:[]},"/api/workspace":{tenantId:"india-paper",runtime:{status:"running"},copilotConfigured:true}};
 const r=await worker.fetch(new Request("https://test/api/workspace",{headers:{Authorization:auth}}),env({body:JSON.stringify(body),source_at:sourceAt}));
 const data=await r.json();assert.equal(data.runtime.status,"snapshot");assert.equal(data.portfolio.holdings[0].fresh,false);assert.equal(data.copilotConfigured,false);assert.equal(data.liveEnabled,false);
 for(const route of ["/api/control","/api/copilot","/api/watchlist"]){assert.equal((await worker.fetch(new Request("https://test"+route,{method:"POST",headers:{Authorization:auth},body:"{}"}),env())).status,405);}
});
