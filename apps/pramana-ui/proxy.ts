import { NextRequest, NextResponse } from "next/server";
import {
  authConfigured,
  SESSION_COOKIE,
  validOrigin,
  validSession,
} from "./lib/auth";
export function proxy(req: NextRequest) {
  const route = req.nextUrl.pathname;
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && !validOrigin(req))
    return NextResponse.json(
      { error: "Invalid request origin" },
      { status: 403 },
    );
  if (route === "/login" || route === "/api/session")
    return NextResponse.next();
  if (!authConfigured())
    return route.startsWith("/api/")
      ? NextResponse.json(
          { error: "Dashboard secret is not configured" },
          { status: 503 },
        )
      : NextResponse.redirect(new URL("/login", req.url));
  if (!validSession(req.cookies.get(SESSION_COOKIE)?.value))
    return route.startsWith("/api/")
      ? NextResponse.json({ error: "Sign in required" }, { status: 401 })
      : NextResponse.redirect(new URL("/login", req.url));
  const response = NextResponse.next();
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("Referrer-Policy", "same-origin");
  return response;
}
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
