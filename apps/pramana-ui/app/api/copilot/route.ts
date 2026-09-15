import { NextRequest, NextResponse } from "next/server";
import { boundedJson } from "@/lib/auth";
import { audit, dailyBudget, rateLimit } from "@/lib/console-db";
import { conversations, generateAnswer } from "@/lib/copilot";
import { tenantId } from "@/lib/db";
export const dynamic = "force-dynamic";
const DEFAULT_DAILY_LIMIT = 200;
const budgetKey = `copilot-day:${tenantId}`;
/** Paid Atlas calls allowed per tenant per UTC day; 0 or less disables the cap. */
function dailyLimit() {
  const raw = Number.parseInt(process.env.PRAMANA_CHAT_DAILY_LIMIT ?? "", 10);
  return Number.isFinite(raw) ? raw : DEFAULT_DAILY_LIMIT;
}
export async function GET() {
  const limit = dailyLimit();
  return NextResponse.json({
    conversations: conversations(),
    configured: !!process.env.ANTHROPIC_API_KEY,
    dailyLimit: limit > 0 ? limit : null,
    dailyRemaining: limit > 0 ? dailyBudget(budgetKey, limit, false).remaining : null,
  });
}
export async function POST(req: NextRequest) {
  if (!rateLimit(`copilot:${tenantId}`, 6))
    return NextResponse.json(
      { error: "Copilot limit reached. Please wait one minute." },
      { status: 429 },
    );
  const limit = dailyLimit();
  if (limit > 0) {
    let budget: ReturnType<typeof dailyBudget>;
    try {
      budget = dailyBudget(budgetKey, limit);
    } catch (e) {
      // Fail closed: an unreadable budget refuses the paid call instead of unbounding spend.
      console.error("copilot daily budget unavailable; refusing request", e);
      return NextResponse.json(
        { error: "Daily Atlas chat budget is unavailable. Request refused." },
        { status: 503 },
      );
    }
    if (!budget.allowed) {
      if (budget.used === limit + 1)
        audit("copilot.budget_exhausted", `Daily Atlas chat budget (${limit}) reached`);
      return NextResponse.json(
        { error: `Daily Atlas chat budget reached (${limit}). Resets at 00:00 UTC.` },
        { status: 429 },
      );
    }
  }
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
    if (body.companyAsOf !== undefined && typeof body.companyAsOf !== "string")
      throw new Error("Invalid company evidence cutoff");
    const row = await generateAnswer(
      body.prompt.trim(),
      body.parentId,
      body.id,
      undefined,
      body.companyAsOf,
      body.brokerCapture,
      body.runComparisonSha256,
    );
    return NextResponse.json(row);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "Invalid request" },
      { status: 400 },
    );
  }
}
