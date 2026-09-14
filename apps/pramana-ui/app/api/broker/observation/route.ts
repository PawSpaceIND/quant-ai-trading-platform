import {NextResponse} from "next/server";
import {readBrokerObservation} from "@/lib/broker-observation";
export const dynamic="force-dynamic";
export function GET(request?:Request) {
  const query=request?new URL(request.url).searchParams:new URLSearchParams();
  const journalId=query.get("journalId"),sequence=query.get("sequence"),captureSha256=query.get("captureSha256");
  if(captureSha256!==null&&(!journalId||!/^[a-f0-9]{64}$/.test(captureSha256)||query.getAll("captureSha256").length!==1))return NextResponse.json({error:"Invalid capture reference"},{status:400,headers:{"Cache-Control":"no-store"}});
  if((journalId!==null||sequence!==null)&&(!journalId||!/^[a-f0-9]{32}$/.test(journalId)||!sequence||!/^([1-9]\d*)$/.test(sequence)||!Number.isSafeInteger(Number(sequence))||query.getAll("journalId").length!==1||query.getAll("sequence").length!==1))
    return NextResponse.json({error:"Select a valid journal and capture sequence."},{status:400,headers:{"Cache-Control":"no-store"}});
  const state=readBrokerObservation(Date.now(),journalId&&sequence?{journalId,sequence:Number(sequence),...(captureSha256?{captureSha256}:{})}:undefined);
  return new NextResponse(JSON.stringify(state,null,2),{status:state.report?200:state.status==="invalid"?503:422,headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store","X-Content-Type-Options":"nosniff",...(state.report?{"Content-Disposition":'attachment; filename="pramana-broker-observation.json"'}:{})}});
}
