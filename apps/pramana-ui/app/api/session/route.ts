import { NextRequest, NextResponse } from "next/server";
import {
  authConfigured,
  boundedJson,
  constantEqual,
  makeSession,
  SESSION_COOKIE,
} from "@/lib/auth";
import { audit, rateLimit } from "@/lib/console-db";
export async function POST(req: NextRequest) {
  if (!authConfigured())
    return NextResponse.json(
      {
        error:
          "Set PRAMANA_DASHBOARD_SECRET to at least 32 characters on the server.",
      },
      { status: 503 },
    );
  if (!rateLimit("login", 10))
    return NextResponse.json(
      { error: "Too many attempts. Try again in one minute." },
      { status: 429 },
    );
  try {
    const body = await boundedJson(req, 2048);
    if (
      typeof body.secret !== "string" ||
      !constantEqual(body.secret, process.env.PRAMANA_DASHBOARD_SECRET!)
    )
      return NextResponse.json(
        { error: "Incorrect access key" },
        { status: 401 },
      );
    const response = NextResponse.json({ ok: true });
    response.cookies.set(SESSION_COOKIE, makeSession(), {
      httpOnly: true,
      sameSite: "strict",
      secure: (process.env.PRAMANA_PUBLIC_ORIGIN || req.url).startsWith(
        "https:",
      ),
      maxAge: 28800,
      path: "/",
    });
    audit("session.login", "Founder session opened");
    return response;
  } catch {
    return NextResponse.json(
      { error: "Invalid sign-in request" },
      { status: 400 },
    );
  }
}
export async function DELETE() {
  const r = NextResponse.json({ ok: true });
  r.cookies.delete(SESSION_COOKIE);
  return r;
}
