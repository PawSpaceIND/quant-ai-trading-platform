import {NextResponse} from "next/server";
import {readPortfolioSnapshot} from "@/lib/portfolio";

export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const state = readPortfolioSnapshot().paperContribution;
    // The download anchor names the evidence file, so a refusal body must announce its own
    // name or it is saved as the export it failed to produce.
    if (!state.report) return NextResponse.json({status: state.status, error: state.detail}, {status: state.status === "unavailable" ? 404 : 503, headers: {"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="pramana-paper-contribution-unavailable.json"'}});
    return new NextResponse(JSON.stringify(state, null, 2), {headers: {"Content-Type": "application/json; charset=utf-8", "Content-Disposition": 'attachment; filename="pramana-paper-contribution.json"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}});
  } catch {
    return NextResponse.json({error: "Paper contribution storage is unavailable."}, {status: 503, headers: {"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="pramana-paper-contribution-unavailable.json"'}});
  }
}
