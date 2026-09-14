import { NextRequest, NextResponse } from "next/server";
import { boundedJson } from "@/lib/auth";
import { rateLimit } from "@/lib/console-db";
import { conversations, generateAnswer } from "@/lib/copilot";
import { tenantId } from "@/lib/db";
export const dynamic = "force-dynamic";
export async function GET() {
  return NextResponse.json({
    conversations: conversations(),
    configured: !!process.env.ANTHROPIC_API_KEY,
  });
}
export async function POST(req: NextRequest) {
  if (!rateLimit(`copilot:${tenantId}`, 6))
    return NextResponse.json(
      { error: "Copilot limit reached. Please wait one minute." },
      { status: 429 },
    );
  try {
    const body = await boundedJson(req);
    if (
      typeof body.prompt !== "string" ||
      !body.prompt.trim() ||
      body.prompt.length > 3000
    )
      throw new Error("Ask a question of 1–3000 characters");
    if (
      body.parentId &&
      (!/^[\w-]{36}$/.test(body.parentId) || !conversations(body.parentId)[0])
    )
      throw new Error("Conversation not found");
    if (body.id && !/^[\w-]{36}$/.test(body.id))
      throw new Error("Invalid request ID");
    const row = await generateAnswer(
      body.prompt.trim(),
      body.parentId,
      body.id,
    );
    return NextResponse.json(row);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "Invalid request" },
      { status: 400 },
    );
  }
}
