import {NextResponse} from "next/server";
import {readRunComparison} from "@/lib/run-comparison";
export const dynamic="force-dynamic";
export function GET(request:Request) {
 const query=new URL(request.url).searchParams,sha=query.get("sha256");
 if(sha!==null&&(!/^[a-f0-9]{64}$/.test(sha)||query.getAll("sha256").length!==1))return NextResponse.json({error:"Invalid comparison reference"},{status:400,headers:{"Cache-Control":"no-store"}});
 const state=readRunComparison(sha??undefined);
 return NextResponse.json(state,{status:state.report?200:state.status==="invalid"?503:422,headers:{"Cache-Control":"no-store","X-Content-Type-Options":"nosniff",...(state.report?{"Content-Disposition":'attachment; filename="pramana-run-comparison.json"'}:{})}});
}
