import {NextResponse} from "next/server";
import {readPortfolioResearch} from "@/lib/research-portfolio";

export const dynamic = "force-dynamic";

// Authenticated by the private workspace proxy; report paths are server configuration.
export async function GET() {
  const state = readPortfolioResearch();
  if (!state.report) return NextResponse.json({error: state.detail}, {
    status: state.status === "unavailable" ? 404 : 503, headers: {"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="pramana-portfolio-replay-unavailable.json"'},
  });
  return new NextResponse(JSON.stringify(state.report, null, 2), {headers: {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Disposition": 'attachment; filename="pramana-portfolio-replay.json"',
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
  }});
}
