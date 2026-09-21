import { NextResponse } from "next/server";
import { readAlerts } from "@/lib/alerts";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Authenticated by the private workspace proxy; the alert log path is server configuration.
export async function GET() {
  try {
    const state = readAlerts();
    return NextResponse.json(state, {
      status: state.status === "invalid" ? 503 : 200,
      headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
    });
  } catch {
    return NextResponse.json({ error: "The alert log is unavailable." }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
