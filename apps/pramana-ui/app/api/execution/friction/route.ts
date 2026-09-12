import { NextResponse } from "next/server";
import { hasTable, openLedger, tenantId } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = openLedger();
  if (!db) return NextResponse.json(empty("ledger_not_found"), { headers: { "Cache-Control": "no-store" } });
  try {
    if (!hasTable(db, "paper_cost_ledger")) return NextResponse.json(empty("cost_ledger_not_found"));
    const rows = db.prepare(
      "SELECT code, SUM(CAST(amount AS REAL)) AS total, COUNT(*) AS entries FROM paper_cost_ledger WHERE tenant_id=? GROUP BY code ORDER BY code",
    ).all(tenantId) as Array<{ code: string; total: number; entries: number }>;
    const byCode = Object.fromEntries(rows.map((row) => [row.code, row.total]));
    const statutoryCodes = ["STT", "SEC", "GST", "SEBI", "STAMP", "EXCHANGE", "FINRA_TAF"];
    const statutoryFees = statutoryCodes.reduce((sum, code) => sum + Number(byCode[code] ?? 0), 0);
    return NextResponse.json({
      status: "ok",
      tenantId,
      slippage: Number(byCode.SLIPPAGE ?? 0),
      spread: Number(byCode.SPREAD ?? 0),
      statutoryFees,
      stt: Number(byCode.STT ?? 0),
      sec: Number(byCode.SEC ?? 0),
      gst: Number(byCode.GST ?? 0),
      byCode,
      entries: rows.reduce((sum, row) => sum + row.entries, 0),
    }, { headers: { "Cache-Control": "no-store" } });
  } finally {
    db.close();
  }
}

function empty(reason: string) {
  return { status: "empty", reason, tenantId, slippage: 0, spread: 0, statutoryFees: 0, stt: 0, sec: 0, gst: 0, byCode: {}, entries: 0 };
}
