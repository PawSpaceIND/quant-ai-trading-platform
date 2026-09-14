import {NextRequest, NextResponse} from "next/server";
import {boundedJson} from "@/lib/auth";
import {rateLimit} from "@/lib/console-db";
import {tenantId} from "@/lib/db";
import {readMarket} from "@/lib/market";
import {nseSymbol, readCompanyEvents, saveCompanyMapping} from "@/lib/company-events";
export const dynamic = "force-dynamic";
export async function GET(req: NextRequest) {
  const at = req.nextUrl.searchParams.get("at") || new Date().toISOString();
  if (!/(Z|[+-]\d\d:\d\d)$/.test(at) || !Number.isFinite(Date.parse(at)) || Date.parse(at) > Date.now() + 1000)
    return NextResponse.json({error: "Choose a valid time no later than now."}, {status: 400});
  const state = readCompanyEvents(at);
  return new NextResponse(JSON.stringify(state), {status: state.status === "invalid" ? 503 : state.status === "unavailable" ? 404 : 200,
    headers: {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      ...(req.nextUrl.searchParams.get("download") === "1" ? {"Content-Disposition": 'attachment; filename="pramana-company-events.json"'} : {})}});
}
export async function POST(req: NextRequest) {
  try {
    if (!rateLimit(`event-mapping:${tenantId}`, 12)) return NextResponse.json({error: "Too many mapping requests. Try again shortly."}, {status: 429});
    const body = await boundedJson(req, 6000);
    const market = await readMarket();
    const mapping = saveCompanyMapping(body, market.rows.map(r => r.symbol).filter(nseSymbol));
    return NextResponse.json({mapping}, {headers: {"Cache-Control": "no-store"}});
  } catch (error) {
    const conflict = error instanceof Error && error.message.startsWith("Mapping changed.");
    return NextResponse.json({error: conflict ? error.message : "Mapping was not saved. Refresh the evidence and review the company, listed NSE symbol and reference."}, {status: conflict ? 409 : 400});
  }
}
