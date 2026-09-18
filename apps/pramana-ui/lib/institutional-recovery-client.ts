/** Server-only, fixed-origin proxy. Credentials never cross the browser boundary. */
import fs from "node:fs";
import path from "node:path";
import {recoveryId, recoveryOutcome, recoveryPayload, recoveryPreview} from "./institutional-recovery-contract";
export class RecoveryGatewayError extends Error {
  constructor(public status: number, public code: string) { super(code); }
}
function configuration() {
  const raw=process.env.PRAMANA_OPERATOR_API_ORIGIN, tenant=process.env.PRAMANA_OPERATOR_TENANT;
  if (!raw || !recoveryId(tenant)) throw new RecoveryGatewayError(503,"recovery_not_configured");
  let url:URL; try { url=new URL(raw); } catch { throw new RecoveryGatewayError(503,"recovery_not_configured"); }
  if (url.origin !== raw || url.username || url.password || url.search || url.hash ||
      (url.protocol !== "https:" && !(url.protocol === "http:" && ["127.0.0.1","[::1]"].includes(url.hostname))))
    throw new RecoveryGatewayError(503,"recovery_not_configured");
  return {origin:url.origin, tenant};
}
function credential(apply:boolean) {
  const name=apply?"PRAMANA_OPERATOR_APPLY_KEY_FILE":"PRAMANA_OPERATOR_READ_KEY_FILE", filename=process.env[name];
  if (!filename || !path.isAbsolute(filename)) throw new RecoveryGatewayError(apply?403:503,"recovery_credential_unavailable");
  let fd:number|undefined;
  try {
    const info=fs.lstatSync(filename);
    if (!info.isFile() || info.isSymbolicLink() || info.nlink!==1 || (info.mode & 0o077)!==0 ||
        info.size>128 || (process.getuid && info.uid!==process.getuid())) throw new Error("Invalid credential file");
    fd=fs.openSync(filename,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
    const opened=fs.fstatSync(fd);
    if (!opened.isFile() || (opened.mode & 0o077)!==0 || opened.uid!==info.uid || opened.dev!==info.dev || opened.ino!==info.ino || opened.nlink!==1 || opened.size!==info.size)
      throw new Error("Credential selection changed");
    const buffer=Buffer.alloc(129),count=fs.readSync(fd,buffer,0,buffer.length,0);
    if(count>128||count!==opened.size)throw new Error("Credential size changed");
    const data=buffer.subarray(0,count).toString("utf8");
    if (!/^[A-Za-z0-9_-]{43}\n?$/.test(data)) throw new Error("Invalid credential");
    return data.trimEnd();
  } catch { throw new RecoveryGatewayError(apply?403:503,"recovery_credential_unavailable"); }
  finally { if(fd!==undefined) fs.closeSync(fd); }
}
async function jsonResponse(response:Response) {
  const reader=response.body?.getReader(); if(!reader) throw new Error("Missing response");
  let size=0; const chunks:Uint8Array[]=[];
  try {
    for(;;) { const next=await reader.read(); if(next.done) break;
      size+=next.value.byteLength; if(size>16384) throw new Error("Response too large"); chunks.push(next.value); }
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
  } finally { await reader.cancel().catch(()=>undefined); }
}
async function send(origin:string, resource:string, key:string, payload?:object) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),8000);
  try {
    const response=await fetch(origin+resource,{method:payload?"POST":"GET",cache:"no-store",redirect:"error",
      signal:controller.signal,headers:{"X-API-Key":key,"Accept":"application/json",...(payload?{"Content-Type":"application/json"}:{})},
      ...(payload?{body:JSON.stringify(payload)}:{})});
    if(!response.ok) { await response.body?.cancel();
      throw new RecoveryGatewayError(payload?502:[401,403,404,409,429].includes(response.status)?response.status:503,
                                     payload?"recovery_outcome_unknown":"recovery_observation_unavailable"); }
    return await jsonResponse(response);
  } catch(error) {
    if(error instanceof RecoveryGatewayError) throw error;
    throw new RecoveryGatewayError(payload?502:503,payload?"recovery_outcome_unknown":"recovery_observation_unavailable");
  } finally { clearTimeout(timer); }
}
export async function inspectRecovery(program:string) {
  if(!recoveryId(program)) throw new RecoveryGatewayError(400,"invalid_recovery_request");
  const selected=configuration(), key=credential(false);
  const value=await send(selected.origin,`/v1/institutional/programs/${encodeURIComponent(program)}/recovery`,key);
  let canApply=false; try { credential(true);canApply=true; } catch { /* Read-only configuration remains useful. */ }
  try { return recoveryPreview(value,selected.tenant,program,canApply); }
  catch { throw new RecoveryGatewayError(503,"recovery_observation_unavailable"); }
}
export async function readRecoveryOutcome(request:string) {
  if(!recoveryId(request)) throw new RecoveryGatewayError(400,"invalid_recovery_request");
  const selected=configuration(), key=credential(false);
  const value=await send(selected.origin,`/v1/institutional/recovery-requests/${encodeURIComponent(request)}`,key);
  try { return recoveryOutcome(value,selected.tenant,request); }
  catch { throw new RecoveryGatewayError(503,"recovery_observation_unavailable"); }
}
export async function applyRecovery(input:unknown) {
  let payload:ReturnType<typeof recoveryPayload>;
  try { payload=recoveryPayload(input); } catch { throw new RecoveryGatewayError(400,"invalid_recovery_request"); }
  const selected=configuration(), key=credential(true);
  // Read and verify the server-selected account/context before sending a mutation.
  const preview=await inspectRecovery(payload.program_id);
  if(preview.context_sha256!==payload.expected_context_sha256) throw new RecoveryGatewayError(409,"recovery_context_changed");
  const current=configuration();
  if(current.origin!==selected.origin||current.tenant!==selected.tenant||credential(true)!==key)
    throw new RecoveryGatewayError(409,"recovery_configuration_changed");
  const {program_id,...body}=payload;
  const value=await send(selected.origin,`/v1/institutional/programs/${encodeURIComponent(program_id)}/recovery`,key,body);
  try { return recoveryOutcome(value,selected.tenant,body.request_id,{program:program_id,context:body.expected_context_sha256}); }
  catch { throw new RecoveryGatewayError(502,"recovery_outcome_unknown"); }
}
