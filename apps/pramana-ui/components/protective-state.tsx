"use client";
import {useState} from "react";
import {gateTally, riskGateViews, type GateState} from "@/lib/risk-gates";
import type {ProtectionAlertState} from "@/lib/protection-sweep";
import type {ProtectionSweep, Runtime} from "@/lib/pilot";
import type {GateRefusalState} from "@/lib/types";

/**
 * Two panels about the same question: what is stopping the engine, and what is not
 * watching at all.
 *
 * `RiskGates` leads with arming because that is the distinction an operator cannot
 * recover from anywhere else. A gate that is off and a gate that is on and content both
 * produce no refusals, and reading the second state off a page that is actually showing
 * the first is how a book ends up with no cross-position limits and a clean dashboard.
 * Every row therefore states one of three words — armed, not armed, unverified — before
 * it states any number.
 *
 * `GateRefusals` shows the journal the engine already writes: the reason strings the
 * gates returned, per decision as well as in total. A tally alone cannot separate "a
 * measurement failed and blocked everything" from "the book is legitimately full".
 */

const badge: Record<GateState, {label: string; className: string}> = {
  armed: {label: "Armed", className: "green"},
  unarmed: {label: "Not armed", className: "neutral"},
  unverified: {label: "Unverified", className: "amber"},
};

const when = (value: string | null | undefined) =>
  value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Unavailable";

/**
 * The unresolved overnight gaps, listed only when the monitor that produces them is
 * armed. An empty list from an unarmed monitor is not a finding and must not render as
 * one, which is why the arming flag gates the whole block rather than the row count.
 */
function GapRows({sweep}: {sweep: ProtectionSweep}) {
  if (!sweep.gapMonitor?.armed) {
    return <p className="muted">Overnight gap escalation is not armed. An undeclared re-basing still suspends the stop; nothing repeats it to you and nothing halts on it.</p>;
  }
  if (!sweep.gapMonitor.unresolved.length) {
    return <p className="muted">No price discontinuity is unresolved. The monitor is armed and has nothing open.</p>;
  }
  return <div className="research-table-scroll"><table>
    <caption>A step larger than the exchange band that no declared corporate action explains. The engine cannot tell a split from a collapse here, so it keeps asking.</caption>
    <thead><tr><th>Symbol</th><th>Verdict</th><th>Step</th><th>Marks</th><th>First seen</th><th>Entries halt</th></tr></thead>
    <tbody>{sweep.gapMonitor.unresolved.slice(0, 20).map((row) => (
      <tr key={row.symbol}>
        <td><strong>{row.symbol}</strong></td>
        <td>{String(row.verdict || "").replaceAll("_", " ").toLowerCase()}{row.nearestAction ? ` · consistent with ${row.nearestAction}` : ""}</td>
        <td className="numeric">{row.stepFraction}</td>
        <td className="numeric">{row.previousMark} → {row.currentMark}</td>
        <td>{when(row.firstSeenAt)}</td>
        <td>{row.haltsAt ? when(row.haltsAt) : "Deadline unavailable"}</td>
      </tr>
    ))}</tbody>
  </table></div>;
}

export function ProtectiveState({runtime, tenant, alert}: {runtime: Runtime; tenant: string; alert: ProtectionAlertState}) {
  const sweep = runtime.protectionSweep;
  const views = riskGateViews(runtime, tenant);
  const tally = gateTally(views);
  return <section className="panel gate-panel">
    <div className="panel-title">
      <div><span className="eyebrow">WHAT IS WATCHING THE BOOK</span><h2>Protective controls</h2></div>
      {views.length ? <span className={`pill ${tally.unverified ? "amber" : tally.armed ? "green" : "neutral"}`}>
        {tally.armed}/{tally.total} armed{tally.unverified ? ` · ${tally.unverified} unverified` : ""}
      </span> : <span className="pill amber">Unpublished</span>}
    </div>
    <div className={`gate-alert ${alert.severity}`} role={alert.severity === "exposed" ? "alert" : "status"}>
      <strong>{alert.headline}</strong>
      <p>{alert.detail}</p>
    </div>
    {sweep ? <GapRows sweep={sweep} /> : null}
    {!views.length ? (
      <p className="empty">The engine published no gate state. Which cross-position, overnight and blackout controls are armed cannot be read from this snapshot, and an absence here is not an all-clear.</p>
    ) : <>
      <p className="research-notice">An unarmed gate and a quiet armed gate look identical in every refusal count on this page. Read the word before the number: a control that is off is not reporting that the book is inside its limits.</p>
      <div className="gate-grid">{views.map((gate) => (
        <article key={gate.id} className={`gate-card ${gate.state}`}>
          <header><strong>{gate.title}</strong><span className={`pill ${badge[gate.state].className}`}>{badge[gate.state].label}</span></header>
          <p className="gate-source">{gate.source}</p>
          {gate.measure ? <p className="gate-measure">{gate.measure}</p> : null}
          <p className="footnote">{gate.limits}</p>
          {gate.active.length ? <ul className="gate-active">{gate.active.map((item) => <li key={item}>In force now: {item}</li>)}</ul> : null}
        </article>
      ))}</div>
    </>}
  </section>;
}

export function GateRefusals({state}: {state?: GateRefusalState}) {
  const [expanded, setExpanded] = useState(false);
  const rows = state?.recent ?? [];
  const shown = expanded ? rows : rows.slice(0, 5);
  return <section className="panel gate-refusals">
    <div className="panel-title">
      <div><span className="eyebrow">JOURNALED ENTRY REFUSALS</span><h2>What the gates refused</h2></div>
      {state?.status === "available"
        ? <span className="pill neutral">{state.rejected ?? "—"} of {state.decisions ?? "—"} decisions</span>
        : <span className="pill amber">Unavailable</span>}
    </div>
    {state?.status !== "available" ? (
      <p className="empty">{state?.detail || "Journaled entry refusals are unavailable."}</p>
    ) : !rows.length ? (
      <p className="muted">{state.detail} The journal holds {state.decisions ?? 0} decisions for this account and none of them was refused. This is a read of the engine&apos;s own record, not an inference from a quiet page.</p>
    ) : <>
      <p className="muted">{state.detail}</p>
      <div className="research-table-scroll"><table>
        <caption>By reason, over the most recent refusals the journal holds (since {when(state.scannedSince)}).</caption>
        <thead><tr><th>Reason</th><th className="numeric">Refusals</th><th>Most recent</th></tr></thead>
        <tbody>{state.reasons.map((row) => (
          <tr key={row.reason}><td><code>{row.reason}</code></td><td className="numeric">{row.count}</td><td>{when(row.lastAt)}</td></tr>
        ))}</tbody>
      </table></div>
      <div className="research-table-scroll"><table>
        <caption>Each refusal as it happened. A reason that suddenly accounts for every entry is a different problem from a book that filled up.</caption>
        <thead><tr><th>When</th><th>Symbol</th><th>Stance</th><th>Reason</th></tr></thead>
        <tbody>{shown.map((row) => (
          <tr key={`${row.decidedAt}:${row.symbol}:${row.reason}`}>
            <td>{when(row.decidedAt)}</td><td><strong>{row.symbol}</strong></td>
            <td>{row.stance || "—"}</td><td><code>{row.reason}</code></td>
          </tr>
        ))}</tbody>
      </table></div>
      {rows.length > 5 ? <button onClick={() => setExpanded(!expanded)}>{expanded ? "Show fewer refusals" : `Show all ${rows.length} recorded refusals`}</button> : null}
    </>}
  </section>;
}
