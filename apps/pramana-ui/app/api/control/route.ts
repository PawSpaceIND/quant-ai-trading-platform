import fs from "node:fs";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";
import { boundedJson } from "@/lib/auth";
import { audit, rateLimit } from "@/lib/console-db";
import { ledgerPath, tenantId } from "@/lib/db";
export async function POST(req: NextRequest) {
  if (!rateLimit(`control:${tenantId}`, 10))
    return NextResponse.json(
      { error: "Please wait before another request" },
      { status: 429 },
    );
  try {
    const body = await boundedJson(req, 3000);
    if (
      body.action !== "halt" ||
      typeof body.reason !== "string" ||
      body.reason.trim().length < 3 ||
      body.reason.length > 500
    )
      throw new Error("A halt reason is required (3–500 characters)");
    const file =
      process.env.PRAMANA_HALT_FILE ||
      path.join(path.dirname(ledgerPath()), "PRAMANA_HALT");
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    audit("halt.requested", body.reason.trim());
    const temporary = file + `.${process.pid}.tmp`;
    fs.writeFileSync(temporary, body.reason.trim(), { mode: 0o600 });
    fs.renameSync(temporary, file);
    return NextResponse.json({
      status: "requested",
      message:
        "Halt requested. Await engine acknowledgement; protective exits continue.",
    });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "Control failed" },
      { status: 400 },
    );
  }
}
