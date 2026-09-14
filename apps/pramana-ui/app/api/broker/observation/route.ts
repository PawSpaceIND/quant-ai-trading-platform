import {NextResponse} from "next/server";
import {readBrokerObservation} from "@/lib/broker-observation";
export const dynamic="force-dynamic";
export function GET() {
  const state=readBrokerObservation();
  return new NextResponse(JSON.stringify(state,null,2),{status:state.report?200:state.status==="invalid"?503:422,headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store","X-Content-Type-Options":"nosniff",...(state.report?{"Content-Disposition":'attachment; filename="pramana-broker-observation.json"'}:{})}});
}
