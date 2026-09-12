import { NextResponse } from "next/server";
import { latestSwarmIntelligence } from "@/lib/proofs";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json(latestSwarmIntelligence(), {
    headers: { "Cache-Control": "no-store" },
  });
}
