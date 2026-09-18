import {NextRequest,NextResponse} from "next/server";
import {authConfigured,boundedJson,SESSION_COOKIE,validOrigin,validSession} from "@/lib/auth";
import {applyRecovery,inspectRecovery,readRecoveryOutcome,RecoveryGatewayError} from "@/lib/institutional-recovery-client";
export const dynamic="force-dynamic";
export const runtime="nodejs";
const headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"};
function authorize(request:NextRequest,write=false) {
  if(!authConfigured()) return NextResponse.json({error:"recovery_not_configured"},{status:503,headers});
  if(!validSession(request.cookies.get(SESSION_COOKIE)?.value)) return NextResponse.json({error:"sign_in_required"},{status:401,headers});
  if(write&&!validOrigin(request)) return NextResponse.json({error:"invalid_request_origin"},{status:403,headers});
  return null;
}
function failure(error:unknown) {
  return NextResponse.json({error:error instanceof RecoveryGatewayError?error.code:"invalid_recovery_request"},
                          {status:error instanceof RecoveryGatewayError?error.status:400,headers});
}
export async function GET(request:NextRequest) {
  const denied=authorize(request);if(denied)return denied;
  const params=[...request.nextUrl.searchParams];
  if(params.length!==1||!["program_id","request_id"].includes(params[0][0])) return failure(new Error());
  try { const [name,id]=params[0]; return NextResponse.json(name==="program_id"?await inspectRecovery(id):await readRecoveryOutcome(id),{headers}); }
  catch(error){return failure(error);}
}
export async function POST(request:NextRequest) {
  const denied=authorize(request,true);if(denied)return denied;
  if(request.nextUrl.search)return failure(new Error());
  try{return NextResponse.json(await applyRecovery(await boundedJson(request,2048)),{headers});}
  catch(error){return failure(error);}
}
