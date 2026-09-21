import {NextResponse} from "next/server";
import {readPortfolioSnapshot} from "@/lib/portfolio";
import {readMarket} from "@/lib/market";
import {historicalRisk} from "@/lib/historical-risk";

export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const market = await readMarket();
    const state = historicalRisk(readPortfolioSnapshot().portfolio,market.riskHistory);
    return new NextResponse(JSON.stringify(state,null,2), {status:state.report ? 200 : state.status === "invalid" ? 503 : 422,
      headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store","X-Content-Type-Options":"nosniff",...(state.report ? {"Content-Disposition":'attachment; filename="pramana-historical-risk.json"'} : {"Content-Disposition":'attachment; filename="pramana-historical-risk-unavailable.json"'})}});
  } catch {return NextResponse.json({error:"Historical risk is unavailable."},{status:503,headers:{"Cache-Control":"no-store","Content-Disposition":'attachment; filename="pramana-historical-risk-unavailable.json"'}});}
}
