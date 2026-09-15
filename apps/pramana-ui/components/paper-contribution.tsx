"use client";

import {useMemo, useState} from "react";
import type {PaperContributionState} from "../lib/paper-contribution";

const currency = new Intl.NumberFormat("en-IN", {style: "currency", currency: "INR", maximumFractionDigits: 2});
const money = (n: number | null) => n === null ? "Unavailable" : currency.format(n);
const pp = (n: number | null) => n === null ? "Unavailable" : `${(n * 100).toFixed(4)} pp`;

export function PaperContribution({state, onAsk}: {state?: PaperContributionState; onAsk: (question: string) => void}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("impact");
  const [page, setPage] = useState(0);
  const report = state?.report;
  const rows = useMemo(() => [...(report?.rows ?? [])].filter(r => `${r.symbol} ${r.assetClass}`.toLowerCase().includes(query.trim().toLowerCase())).sort((a,b) => {
    if (sort === "symbol") return a.symbol.localeCompare(b.symbol) || a.assetClass.localeCompare(b.assetClass);
    const amount = (r: typeof a) => sort === "fees" ? r.cashFees : r.netPnl === null ? -Infinity : Math.abs(r.netPnl);
    return amount(b) - amount(a) || a.symbol.localeCompare(b.symbol);
  }), [report, query, sort]);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(rows.length / 25) - 1));
  const start = currentPage * 25;
  return <section className="panel portfolio-replay paper-contribution">
    <div className="panel-title"><div><span className="eyebrow">RECORDED PAPER ACCOUNT</span><h2>What drove your P&amp;L</h2></div><span className="muted">{state?.status ?? "unavailable"}</span></div>
    <p className={state?.status === "available" ? "muted" : "research-notice"}>{state?.detail ?? "Account contribution has not been loaded."}</p>
    {report && <>
      <p className="muted">Valuation {new Date(report.asOf).toLocaleString()} · Ledger {report.ledgerId} · {report.fillCount} fills · {report.rows.length} instruments</p>
      <dl className="research-stats replay-metrics">
        <div><dt>Realized before cash fees</dt><dd>{money(report.totals.realizedGrossPnl)}</dd></div>
        <div><dt>All recorded cash fees</dt><dd>{money(report.totals.cashFees)}</dd></div>
        <div><dt>Realized after all cash fees</dt><dd>{money(report.totals.realizedAfterFeesPnl)}</dd></div>
        <div><dt>Current unrealized P&amp;L</dt><dd>{money(report.totals.unrealizedPnl)}</dd></div>
        <div><dt>Current net contribution</dt><dd>{money(report.totals.netPnl)} / {pp(report.totals.returnContribution)}</dd></div>
        <div><dt>Equity reconciliation difference</dt><dd>{money(report.reconciliationDifference)}</dd></div>
      </dl>
      <p className="footnote">Realized P&amp;L − all cash fees + unrealized P&amp;L = net contribution. Fees are expensed immediately, including fees on open buys. Average entry excludes fees. Net P&amp;L already includes recorded costs.</p>
      <div className="replay-selectors">
        <label>Find instrument<input aria-label="Find contribution instrument" value={query} onChange={e => {setQuery(e.target.value); setPage(0);}} placeholder="Symbol or asset class" /></label>
        <label>Order<select aria-label="Paper contribution order" value={sort} onChange={e => {setSort(e.target.value); setPage(0);}}><option value="impact">Largest absolute net contribution</option><option value="fees">Highest recorded cash fees</option><option value="symbol">Symbol</option></select></label>
      </div>
      <p className="footnote">{rows.length} matching instruments. Summary totals cover all {report.rows.length} instruments, including closed positions. Return contributions use initial account capital of {money(report.startingCapital)}.</p>
      <div className="research-table-scroll" role="region" tabIndex={0} aria-label="Paper instrument contributions">
        <table><caption>Paper instrument contributions</caption><thead><tr>{["Symbol", "Asset", "Open quantity", "Mark state", "Realized before fees", "Cash fees", "Unrealized P&L", "Net P&L", "Return contribution", "Modeled spread", "Modeled slippage"].map(c => <th key={c} scope="col">{c}</th>)}</tr></thead>
          <tbody>{rows.slice(start, start + 25).map(r => <tr key={`${r.market}:${r.assetClass}:${r.symbol}`}><th scope="row">{r.symbol}</th><td>{r.assetClass}</td><td>{r.quantity}</td><td>{r.markState === "closed" ? "Closed" : r.markState === "fresh" ? "Fresh tick" : "Unavailable"}</td><td>{money(r.realizedGrossPnl)}</td><td>{money(r.cashFees)}</td><td>{money(r.unrealizedPnl)}</td><td>{money(r.netPnl)}</td><td>{pp(r.returnContribution)}</td><td>{money(r.spread)}</td><td>{money(r.slippage)}</td></tr>)}</tbody>
        </table>
      </div>
      {rows.length ? <div className="replay-pagination"><span>{start + 1}–{Math.min(start + 25, rows.length)} of {rows.length}</span><button aria-label="Previous paper contributions" disabled={!currentPage} onClick={() => setPage(currentPage - 1)}>Previous</button><button aria-label="Next paper contributions" disabled={start + 25 >= rows.length} onClick={() => setPage(currentPage + 1)}>Next</button></div> : <p className="empty">{query ? "No instruments match this filter." : "No recorded fills. Cash alone has no instrument contribution."}</p>}
      <details><summary>Recorded execution costs and evidence</summary>
        <dl className="research-stats replay-metrics"><div><dt>Modeled spread</dt><dd>{money(report.totals.spread)}</dd></div><div><dt>Modeled slippage</dt><dd>{money(report.totals.slippage)}</dd></div><div><dt>P&amp;L before modeled costs</dt><dd>{money(report.totals.beforeModeledCostsPnl)}</dd></div></dl>
        <p className="footnote">The cost add-back uses the same executed quantities and final marks. Spread/slippage are recorded simulation estimates, not measured exchange fills or an achievable alternative result. Cash fees are added back once.</p>
        <p>Missing spread records: {report.costCoverage.missingSpreadOrders} orders · Missing slippage records: {report.costCoverage.missingSlippageOrders} orders · Unknown noncash cost rows: {report.costCoverage.unknownNoncashRows}</p>
        <ul>{report.limitations.map(line => <li key={line}>{line}</li>)}</ul>
        <p className="footnote">Source SHA-256: <code>{report.sourceSha256}</code></p>
      </details>
      <div className="research-actions"><button onClick={() => onAsk("Explain the recorded paper account contribution, including closed positions, missing marks and recorded cost coverage. Reconcile realized P&L minus all cash fees plus unrealized P&L to net contribution. Fees on open buys are already expensed; do not subtract costs twice. Distinguish recorded model spread/slippage from observed execution quality, and account results from research simulations or benchmark outperformance.")}>Discuss account contribution with Atlas ↗</button><a href="/api/portfolio/contribution" download="pramana-paper-contribution.json">Export account contribution ↓</a></div>
    </>}
  </section>;
}
