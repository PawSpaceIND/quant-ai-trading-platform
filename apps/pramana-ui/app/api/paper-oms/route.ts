import {NextRequest, NextResponse} from "next/server";
import {authConfigured, SESSION_COOKIE, validSession} from "@/lib/auth";
import {readPaperOms} from "@/lib/paper-oms";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
const headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"};
/** Defense in depth: authenticate here, not just in the shared proxy. No mutation route. */
export async function GET(request: NextRequest) {
  if (!authConfigured()) return NextResponse.json({error: "Dashboard secret is not configured"}, {status: 503, headers});
  if (!validSession(request.cookies.get(SESSION_COOKIE)?.value))
    return NextResponse.json({error: "Sign in required"}, {status: 401, headers});
  if (request.nextUrl.search) return NextResponse.json({error: "This observation accepts no tenant, path or action parameters"}, {status: 400, headers});
  const observation = readPaperOms();
  return NextResponse.json(observation, {status: observation.status === "unavailable" ? 503 : 200, headers});
}
