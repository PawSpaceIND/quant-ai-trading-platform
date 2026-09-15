import {NextResponse} from "next/server";
import {readBenchmarkAttribution} from "@/lib/benchmark-attribution-store";
export const dynamic="force-dynamic";
// The private-workspace proxy authenticates every API request. Only the operator's
// selected report is exposed; browser-supplied filesystem paths are never accepted.
export async function GET() {
  const state=readBenchmarkAttribution();
  if(!state.report) return NextResponse.json({error:state.detail},{status:state.status==="unavailable"?404:503,headers:{"Cache-Control":"no-store"}});
  return new NextResponse(JSON.stringify({report:state.report,limitations:state.detail},null,2),{headers:{
    "Content-Type":"application/json; charset=utf-8",
    "Content-Disposition":'attachment; filename="pramana-benchmark-attribution.json"',
    "Cache-Control":"no-store","X-Content-Type-Options":"nosniff",
  }});
}
