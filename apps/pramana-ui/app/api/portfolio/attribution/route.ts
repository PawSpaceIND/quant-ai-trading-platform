import {NextResponse} from "next/server";
import {readPortfolioSnapshot} from "@/lib/portfolio";
import {portfolioAttribution} from "@/lib/portfolio-attribution";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const state = portfolioAttribution(readPortfolioSnapshot().portfolio);
    return new NextResponse(JSON.stringify(state, null, 2), {
      status: state.status === "available" ? 200 : 422,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        ...(state.status === "available" ? {"Content-Disposition": 'attachment; filename="pramana-portfolio-attribution.json"'} : {"Content-Disposition": 'attachment; filename="pramana-portfolio-attribution-unavailable.json"'}),
      },
    });
  } catch {
    return NextResponse.json({error: "Portfolio attribution is unavailable."}, {status: 503, headers: {"Cache-Control": "no-store"}});
  }
}
