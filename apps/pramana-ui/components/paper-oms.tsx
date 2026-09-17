"use client";
import {useCallback, useEffect, useRef, useState} from "react";
import type {PaperOmsObservation} from "@/lib/paper-oms-types";

export function PaperOmsPanel() {
  const [observation, setObservation] = useState<PaperOmsObservation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setLoading(true); setObservation(null); setError("");
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/api/paper-oms", {cache: "no-store", signal: controller.signal});
      const value = await response.json();
      if (active.current !== controller) return;
      if ((response.ok || response.status === 503) && value.schema === "pramana.paper_oms_observation.v1")
        setObservation(value);
      else setError(response.status === 401 ? "Sign in again to inspect paper orders."
        : "Paper order observation is unavailable. No recovery or clean-account claim is made.");
    } catch {
      if (active.current === controller) setError("Paper order observation is unavailable. Refresh to inspect again.");
    } finally {
      clearTimeout(timer);
      if (active.current === controller) setLoading(false);
    }
  }, []);
  useEffect(() => { void refresh(); return () => { active.current?.abort(); active.current = null; }; }, [refresh]);
  return <section className="panel" aria-label="Paper order recovery inspection">
    <div className="panel-heading" style={{display:"flex",gap:"1rem",justifyContent:"space-between",flexWrap:"wrap"}}>
      <div><span className="eyebrow">LOCAL PAPER OMS · READ ONLY</span><h2>Paper order recovery</h2></div>
      <button className="btn" type="button" onClick={() => void refresh()} disabled={loading}>
        {loading ? "Inspecting paper orders…" : "Refresh paper orders"}
      </button>
    </div>
    <p className="muted">Stored order status is not verified recovery. This panel cannot submit, cancel, retry, recover a trade or clear a halt.</p>
    <div aria-live="polite">
      {error && <p className="callout">{error}</p>}
      {observation && <>
        <p className="callout">{observation.reason}</p>
        <p className="footnote">Observed {observation.observedAt}. Point-in-time display; refresh for a new observation. No launch approval is provided.</p>
        {observation.status === "observed" && <>
          <p><strong>{observation.openOrders}</strong> open stored orders · {observation.totalOrders} total stored orders · {observation.recordedRecoveryAudits} recorded recovery audits</p>
          {observation.orders.length === 0
            ? <p className="empty">No open order rows in this snapshot. This does not prove that every broker fill, receipt or accounting obligation is reconciled.</p>
            : <div style={{display:"grid",gap:"0.75rem"}}>
              {observation.orders.map(order => <article className="callout" key={order.clientOrderId}>
                <div style={{display:"flex",gap:"0.5rem",justifyContent:"space-between",flexWrap:"wrap"}}>
                  <strong>{order.symbol} · {order.side}</strong><span className="pill amber">{order.state}</span>
                </div>
                <p>Requested {order.requestedQuantity} · Filled {order.filledQuantity}</p>
                <p className="footnote" style={{overflowWrap:"anywhere"}}>Order reference: <code>{order.clientOrderId}</code></p>
              </article>)}
            </div>}
          {observation.truncated && <p className="callout">Only the first {observation.orders.length} open orders are shown; the open-order count includes the full bounded snapshot.</p>}
        </>}
      </>}
    </div>
  </section>;
}
