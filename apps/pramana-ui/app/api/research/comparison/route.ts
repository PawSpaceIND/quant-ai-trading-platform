import { NextResponse } from "next/server";
import { readResearchLab } from "@/lib/research-lab";

export const dynamic = "force-dynamic";

// The existing private-workspace proxy authenticates this route. No path or
// experiment identifier is accepted from the browser; the operator binds one report.
export async function GET() {
  const state = readResearchLab();
  if (!state.report) return NextResponse.json({error: state.detail}, {
    status: state.status === "unavailable" ? 404 : 503,
    headers: {"Cache-Control": "no-store"},
  });
  return new NextResponse(JSON.stringify(state.report, null, 2), {headers: {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Disposition": 'attachment; filename="pramana-experiment-comparison.json"',
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  }});
}
