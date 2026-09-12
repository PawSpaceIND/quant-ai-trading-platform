import { NextResponse } from "next/server";
import { readPortfolio } from "@/lib/portfolio";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json(readPortfolio(), {
    headers: { "Cache-Control": "no-store" },
  });
}
