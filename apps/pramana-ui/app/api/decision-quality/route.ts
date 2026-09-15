import { NextResponse } from "next/server";
import { readDecisionQuality, readPostMortems, verdictFor } from "@/lib/decision-quality";

export const dynamic = "force-dynamic";

// Authenticated by the private workspace proxy; report and post-mortem paths are server configuration.
export async function GET() {
  try {
    const report = readDecisionQuality();
    const postMortems = readPostMortems();
    return NextResponse.json(
      { report, postMortems, verdict: report ? verdictFor(report) : null },
      { headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } },
    );
  } catch {
    return NextResponse.json({ error: "Decision quality is unavailable." }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
