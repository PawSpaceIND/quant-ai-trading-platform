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
      { status: 403, headers: {"Cache-Control":"no-store"} },
    );
  if (route === "/login" || route === "/api/session") return secured(NextResponse.next());
  if (!authConfigured())
    return route.startsWith("/api/")
      ? NextResponse.json(
          { error: "Dashboard secret is not configured" },
          { status: 503, headers: {"Cache-Control":"no-store"} },
        )
      : NextResponse.redirect(signIn(req));
  if (!validSession(req.cookies.get(SESSION_COOKIE)?.value))
    return route.startsWith("/api/")
      ? NextResponse.json({ error: "Sign in required" }, { status: 401, headers: {"Cache-Control":"no-store"} })
      : NextResponse.redirect(signIn(req));
  return secured(NextResponse.next());
}

/** Sign-in, remembering where the operator was so the round trip does not lose it. */
function signIn(req: NextRequest) {
  const target = new URL("/login", req.url);
  const from = req.nextUrl.pathname + req.nextUrl.search;
  // Same-origin paths only: never a protocol-relative or absolute destination.
  if (from.startsWith("/") && !from.startsWith("//") && from !== "/")
    target.searchParams.set("next", from);
  return target;
}

/** The headers every response carries, the sign-in page included. */
function secured(response: NextResponse) {
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("Referrer-Policy", "same-origin");
  return response;
}
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
