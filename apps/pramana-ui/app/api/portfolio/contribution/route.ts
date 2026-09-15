import {NextResponse} from "next/server";
import {readPortfolioSnapshot} from "@/lib/portfolio";

export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const state = readPortfolioSnapshot().paperContribution;
    if (!state.report) return NextResponse.json({status: state.status, error: state.detail}, {status: state.status === "unavailable" ? 404 : 503, headers: {"Cache-Control": "no-store"}});
    return new NextResponse(JSON.stringify(state, null, 2), {headers: {"Content-Type": "application/json; charset=utf-8", "Content-Disposition": 'attachment; filename="pramana-paper-contribution.json"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}});
  } catch {
    return NextResponse.json({error: "Paper contribution storage is unavailable."}, {status: 503, headers: {"Cache-Control": "no-store"}});
  }
}
