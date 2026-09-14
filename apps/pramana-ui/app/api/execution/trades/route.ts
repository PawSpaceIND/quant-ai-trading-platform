import { NextResponse } from "next/server";
import { hasTable, LedgerRow, openLedger, tenantId } from "@/lib/db";
import { proofsByOrderId } from "@/lib/proofs";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = openLedger();
  if (!db) return NextResponse.json({ status: "empty", reason: "ledger_not_found", trades: [] });
  try {
    if (!hasTable(db, "paper_ledger")) return NextResponse.json({ status: "empty", reason: "paper_ledger_not_found", trades: [] });
    const trades = db.prepare(
      "SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id DESC LIMIT 100",
    ).all(tenantId) as LedgerRow[];
    const byOrder = proofsByOrderId();
    return NextResponse.json({
      status: "ok",
      tenantId,
      trades: trades.map((trade) => {
        const match = byOrder.get(trade.order_id);
        return {
          orderId: trade.order_id,
          symbol: trade.symbol,
          market: trade.market,
          assetClass: trade.asset_class,
          side: trade.side,
          quantity: trade.quantity,
          fillPrice: Number(trade.fill_price),
          notional: Number(trade.notional),
          status: trade.status,
          createdAt: trade.created_at,
          proof: match ? {
            file: match.file,
            decisionId: match.proof.decision_id ?? null,
            rationale: match.proof.declared_rationales ?? [],
            stress: match.proof.stress_verdict ?? {},
            risk: match.proof.risk_verdict ?? {},
          } : null,
          proofStatus: match ? "exact_match" : "unavailable_no_exact_reference",
        };
      }),
    }, { headers: { "Cache-Control": "no-store" } });
  } finally {
    db.close();
  }
}
