import {NextResponse} from "next/server";
import {readExternalAccount} from "@/lib/external-account";
export const dynamic="force-dynamic";
// The workspace proxy authenticates this route; the browser provides no file path.
export async function GET(){
 const state=readExternalAccount();
 if(!state.report)return NextResponse.json({error:state.detail},{status:state.status==="unavailable"?404:503,headers:{"Cache-Control":"no-store"}});
 return new NextResponse(JSON.stringify(state.report,null,2),{headers:{"Content-Type":"application/json; charset=utf-8","Content-Disposition":'attachment; filename="pramana-external-account.json"',"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"}});
}
