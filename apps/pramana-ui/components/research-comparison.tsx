"use client";

import type { ResearchLabState } from "../lib/research-lab";

const inr = (value: string | null) => value === null ? "Unavailable" : new Intl.NumberFormat("en-IN", {style: "currency", currency: "INR", maximumFractionDigits: 2}).format(Number(value));
export function ResearchComparison({state, onAsk}: {state?: ResearchLabState; onAsk: (question: string) => void}) {
  const r = state?.report;
  return <section className="panel research-comparison">
    <div className="panel-title"><div><span className="eyebrow">CONTROLLED EXPERIMENTS</span><h2>Candidate comparison</h2></div><span className="muted">Review only</span></div>
    {!r ? <p className="empty">{state?.detail || "No experiment comparison has been published to this workspace."}</p> : <>
      <h3>{r.experiment}</h3>
      <p className="muted">{r.mode === "historical" ? "Historical evaluation" : "Forward paper evaluation"} · Protocol {r.protocol_version} · Published {new Date(r.generated_at).toLocaleString()}</p>
      <p className="research-notice"><strong>Insufficient evidence. No strategy is approved by this report.</strong> Each case starts with {inr(r.costs.capital_per_case)}. Completed-case P&amp;L is not a portfolio return; missing decisions and unresolved exits may conceal losses. API cost remains separate in USD.</p>
      <p>{r.registered_cases} / {r.expected_cases} cases collected · {r.resolved_cases} outcomes recorded · {r.unverified_adjustments} cases with unverified adjustments</p>
      <details open><summary>Comparison blockers ({r.comparison_blockers.length})</summary>
        {r.comparison_blockers.length ? <ul>{r.comparison_blockers.map((item, i) => <li key={i}>{item.replaceAll("_", " ")}</li>)}</ul> : <p className="muted">No completeness blockers in this snapshot. Source quality, selection bias and independent validation still need review.</p>}
      </details>
      <div className="research-candidates">{Object.entries(r.candidates).map(([name, c]) => <article className="research-candidate" key={name}>
        <h3>{name}{name === r.baseline && <small> · Baseline</small>}</h3>
        <p className="footnote">Declared/requested model: {c.model_version || "No decision recorded"} · Returned identities: {c.returned_models.join(", ") || "Not recorded"} · Prompt: {c.prompt_version || "Unavailable"}</p>
        <dl className="research-stats">
          <div><dt>Completed-case P&amp;L</dt><dd>{inr(c.completed_case_pnl_inr)}</dd></div>
          <div><dt>Completed cases</dt><dd>{c.completed_episodes}</dd></div>
          <div><dt>Decisions / missing</dt><dd>{c.decisions} / {c.missing_decisions}</dd></div>
          <div><dt>Provider / invalid results</dt><dd>{c.errors}</dd></div>
          <div><dt>No-trade decisions</dt><dd>{c.holds}</dd></div>
          <div><dt>Pending buys / unresolved exits</dt><dd>{c.pending_buy_outcomes} / {c.unresolved_exits}</dd></div>
          <div><dt>Unfilled / partial entries</dt><dd>{c.unfilled} / {c.partial_entries}</dd></div>
          <div><dt>Known API cost subtotal (USD)</dt><dd>${Number(c.api_cost_usd).toFixed(6)}{!c.cost_total_complete && " · Incomplete"}</dd></div>
          <div><dt>Decisions with unknown cost</dt><dd>{c.unknown_cost_decisions}</dd></div>
          <div><dt>Total reported latency</dt><dd>{(Number(c.total_latency_ms) / 1000).toFixed(2)}s</dd></div>
        </dl>
        <details><summary>Cost sensitivity and execution cases</summary>
          <p className="footnote">Base assumptions: {r.costs.fee_bps} bps fees + {r.costs.slippage_bps} bps adverse slippage. Each multiplier recalculates cash-limited quantity. Coverage can change.</p>
          <div className="research-table-scroll" tabIndex={0} role="region" aria-label={`${name} cost sensitivity`}><table><caption>Declared fees and slippage sensitivity</caption><thead><tr><th scope="col">Costs</th><th scope="col">Completed P&amp;L</th><th scope="col">Completed</th><th scope="col">Unresolved</th><th scope="col">Unfilled</th></tr></thead><tbody>
            {c.cost_stress.map(s => <tr key={s.multiplier}><th scope="row">{s.multiplier}×</th><td>{inr(s.completed_case_pnl_inr)}</td><td>{s.completed_cases ?? "—"}</td><td>{s.unresolved_exits ?? "—"}</td><td>{s.unfilled ?? "—"}</td></tr>)}
          </tbody></table></div>
          {c.outcomes.length ? <div className="research-table-scroll" tabIndex={0} role="region" aria-label={`${name} execution cases`}><table><caption>Recorded execution cases</caption><thead><tr><th scope="col">Case</th><th scope="col">Outcome</th><th scope="col">Quantity</th><th scope="col">Net P&amp;L</th></tr></thead><tbody>{c.outcomes.map(o => <tr key={o.case_id}><th scope="row">{o.case_id}</th><td>{o.status.replaceAll("_", " ")}</td><td>{o.filled_quantity}</td><td>{inr(o.net_pnl_inr)}</td></tr>)}</tbody></table></div> : <p className="muted">No BUY execution cases are available. No-trade decisions and failures are counted above.</p>}
        </details>
      </article>)}</div>
      <details><summary>Assumptions and evidence limits</summary><ul>{r.limitations.map((text, i) => <li key={i}>{text}</li>)}</ul><p className="footnote">Private source evidence SHA-256: <code>{r.evidence_sha256}</code>. This identifies the reviewed snapshot; it is not an independent signature or proof of capture time.</p></details>
      <div className="research-actions"><button onClick={() => onAsk(`Explain the published candidate comparison for experiment ${JSON.stringify(r.experiment)}. Compare decision coverage, unresolved losses, costs and sensitivity. Identify what is still needed for validation; do not select a profitable winner from these case totals.`)}>Discuss comparison with Atlas ↗</button><a href="/api/research/comparison" download="pramana-experiment-comparison.json">Export comparison ↓</a></div>
    </>}
  </section>;
}
