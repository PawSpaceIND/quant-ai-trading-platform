import {snapshotHealth} from "./health.mjs";
import {workspaceSnapshot} from "./workspace.mjs";
const paths = ["/api/market","/api/portfolio/mtm","/api/intelligence/swarm","/api/execution/friction","/api/execution/trades"];
async function equal(a,b) {
 if (!a || !b) return false;
 const enc=new TextEncoder();
 const [x,y]=await Promise.all([crypto.subtle.digest("SHA-256",enc.encode(a)),crypto.subtle.digest("SHA-256",enc.encode(b))]);
 const u=new Uint8Array(x),v=new Uint8Array(y);let diff=0;for(let i=0;i<u.length;i++)diff|=u[i]^v[i];return diff===0;
}
function response(body,status=200,headers={}) {
 return new Response(body,{status,headers:{"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer","X-Frame-Options":"DENY",...headers}});
}
function json(value,status=200){return response(JSON.stringify(value),status,{"Content-Type":"application/json"});}
export default {
 async fetch(request,env) {
  const url=new URL(request.url);
  if(url.pathname==="/healthz"){
   if(!env.MONITOR_TOKEN || !await equal(request.headers.get("Authorization"),"Bearer "+env.MONITOR_TOKEN))return response("Unauthorized",401);
   if(request.method!=="GET" && request.method!=="HEAD")return response("Method not allowed",405);
   try {
    const row=await env.DB.prepare("SELECT body,source_at,received_at FROM snapshot WHERE id=1").first();
    const health=snapshotHealth(row);
    return request.method==="HEAD" ? response(null,health.status==="observation_ok"?200:503) : json(health,health.status==="observation_ok"?200:503);
   } catch { return json({status:"unhealthy",reasons:["monitor_storage_unavailable"]},503); }
  }
  if(url.pathname==="/_ingest"){
   if(request.method!=="POST")return response("Method not allowed",405);
   if(!await equal(request.headers.get("Authorization"),"Bearer "+env.PUBLISH_TOKEN) || !env.PUBLISH_TOKEN)return response("Unauthorized",401);
   const reader=request.body?.getReader();if(!reader)return response("Missing body",400);
   const chunks=[];let total=0;
   while(true){const {done,value}=await reader.read();if(done)break;total+=value.length;if(total>1500000){await reader.cancel();return response("Too large",413);}chunks.push(value);}
   let payload;try{const bytes=new Uint8Array(total);let pos=0;for(const chunk of chunks){bytes.set(chunk,pos);pos+=chunk.length;}payload=JSON.parse(new TextDecoder().decode(bytes));}catch{return response("Invalid JSON",400);}
   if(!payload || typeof payload!=="object" || paths.some(p=>!payload.snapshots?.[p] || typeof payload.snapshots[p]!=="object"))return response("Incomplete snapshot",400);
   if(payload.snapshots["/api/portfolio/mtm"].tenantId!=="india-paper")return response("Wrong paper tenant",400);
   const at=Date.parse(payload.sourceAt);if(!Number.isFinite(at)||Math.abs(Date.now()-at)>300000)return response("Invalid source time",400);
   const snapshots=Object.fromEntries(paths.map(p=>[p,payload.snapshots[p]]));
   if(payload.snapshots["/api/workspace"]?.tenantId==="india-paper") {
    const {researchLab,researchPortfolio, companyEvents,audit,...workspace}=payload.snapshots["/api/workspace"];
    snapshots["/api/workspace"]={...workspace,audit:[]};
   }
   await env.DB.prepare("INSERT INTO snapshot(id,body,source_at,received_at) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,source_at=excluded.source_at,received_at=excluded.received_at WHERE excluded.source_at >= snapshot.source_at").bind(JSON.stringify(snapshots),new Date(at).toISOString(),new Date().toISOString()).run();
   return json({status:"saved"});
  }
  if(!await equal(request.headers.get("Authorization"),env.VIEW_AUTH))return response("Pramana private paper dashboard. Sign in to continue.",401,{"WWW-Authenticate":'Basic realm="Pramana", charset="UTF-8"'});
  if(request.method!=="GET" && request.method!=="HEAD")return response("Read-only site",405);
  if(url.pathname.startsWith("/api/")){
   if(!paths.includes(url.pathname)&&!["/api/cloud-status","/api/workspace","/api/watchlist","/api/copilot"].includes(url.pathname))return json({error:"not_found"},404);
   const row=await env.DB.prepare("SELECT body,source_at,received_at FROM snapshot WHERE id=1").first();
   if(url.pathname==="/api/cloud-status")return json({status:row?"ok":"waiting",sourceAt:row?.source_at??null,receivedAt:row?.received_at??null,stale:!row||Date.now()-Date.parse(row.source_at)>180000,mode:"paper",engineHost:"Mac"});
   if(url.pathname==="/api/watchlist")return json({symbols:[],readOnly:true});
   if(url.pathname==="/api/copilot")return json({conversations:[],configured:false,readOnly:true});
   if(!row)return json({status:"unavailable",reason:"awaiting_paper_snapshot"},503);
   const snapshots=JSON.parse(row.body);
   if(url.pathname==="/api/workspace")return json(workspaceSnapshot(snapshots,row.source_at));
   const data=snapshots[url.pathname];return json(data);
  }
  const asset=await env.ASSETS.fetch(request);
  const result=new Response(asset.body,asset);
  result.headers.set("Cache-Control","private, no-store");result.headers.set("X-Frame-Options","DENY");result.headers.set("Referrer-Policy","no-referrer");result.headers.set("X-Content-Type-Options","nosniff");return result;
 }
};
