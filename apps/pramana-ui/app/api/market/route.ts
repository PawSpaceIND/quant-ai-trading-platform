import { NextResponse } from "next/server";
import { readMarket } from "@/lib/market";
export const dynamic = "force-dynamic";
export async function GET() {
  // The daily-close series is engine input, not display data. The workspace route and the
  // copilot both strip it; this route is on the hosted worker's public allow-list, so it
  // must strip it too rather than mirror it to the published snapshot.
  const { riskHistory: _withheld, ...market } = await readMarket();
  return NextResponse.json(market, {
    headers: { "Cache-Control": "no-store" },
  });
}
