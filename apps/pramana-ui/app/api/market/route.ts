import { NextResponse } from "next/server";
import { readMarket } from "@/lib/market";
export const dynamic = "force-dynamic";
export async function GET() {
  return NextResponse.json(await readMarket(), {
    headers: { "Cache-Control": "no-store" },
  });
}
