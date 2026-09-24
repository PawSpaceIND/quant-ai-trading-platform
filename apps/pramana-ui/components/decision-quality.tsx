"use client";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { AiBudget, CalibrationBin, DecisionQualityReport, ForecastBasisRow, ForecastScoring, HoldCauses, InferenceHealth, MissedOpportunities, PostMortem, RecentDecision, Significance, TStatistic, Verdict } from "@/lib/decision-quality-model";
import { DASH, INFERENCE_STATUS_LABELS, RATE_MINIMUM, count, hourLabel, minutes, money, percent, rateShown, ratio, signedMoney, signedPercent, signedRatio, stancesSummary, thinNote, thinRate } from "@/lib/decision-quality-model";

type QualityResponse = { report: DecisionQualityReport | null; postMortems: PostMortem[]; missed?: MissedOpportunities | null; verdict: Verdict | null };

/** Relative age of a report, so a verdict is never read as current by default. */
const age = (value: string | null | undefined) => {
  const at = value ? Date.parse(value) : NaN;
  if (!Number.isFinite(at)) return "";
  const minutes = Math.max(0, Math.round((Date.now() - at) / 60000));
  if (minutes < 60) return ` \u00b7 ${minutes} min old`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? ` \u00b7 ${hours} h old` : ` \u00b7 ${Math.round(hours / 24)} days old`;
};
const when = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "—";
const day = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "—";
const signedClass = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? "" : v >= 0 ? "positive" : "negative";

async function request(url: string): Promise<QualityResponse> {
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(12000) });
  if (response.status === 401) { window.location.assign("/login"); throw new Error("Session expired"); }
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Decision-quality request failed");
  return body;
}

/** Read-only view of the engine's calibration report and session post-mortems. Approval stays on the host CLI. */
export function DecisionQuality() {
  const [state, setState] = useState<QualityResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    try { setState(await request("/api/decision-quality")); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "Decision quality could not be loaded"); }
    finally { setBusy(false); }
  }, []);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 60_000);
    return () => clearInterval(timer);
  }, [load]);
  const report = state?.report ?? null;
  const verdict = state?.verdict ?? null;
  return <>
    {error && <div className="banner error" role="alert">{error}. Last displayed evidence may be stale. <button onClick={() => void load()} disabled={busy}>Retry</button></div>}
    {!state && !error && <div className="loading-state" role="status">Loading decision quality…</div>}
    {state && !report && <section className="panel" aria-label="Decision quality">
      <div className="panel-title"><div><span className="eyebrow">CALIBRATION · OUTCOMES</span><h2>Decision quality</h2></div><button onClick={() => void load()} disabled={busy}>↻ Refresh</button></div>
      <div className="empty">No usable decision-quality report for this workspace. The report may be missing, invalid or belong to another account; no performance verdict is inferred.</div>
    </section>}
    {report && verdict && <>
      <VerdictBanner verdict={verdict} />
      {report.ai_budget && <AiBudgetNotice budget={report.ai_budget} />}
      <div className="quality-meta">
        <span>Report generated {when(report.generated_at)}{age(report.generated_at)}</span>
        <span>Window {day(report.window.since)} → {day(report.window.until)} · {count(report.window.sessions)} sessions</span>
        <span>Tenant {report.tenant_id}</span>
        {report.by_mode.length > 0 && <span>By mode: {report.by_mode.map((m) => `${m.mode} ${count(m.decisions)}`).join(" · ")}</span>}
        <button onClick={() => void load()} disabled={busy} aria-label="Refresh decision quality">↻ Refresh</button>
      </div>
      <Headline report={report} />
      <HoldCausesPanel holds={report.holds} decisions={report.counts.decisions} />
      <ModelDecisionHealth report={report.inference_health} />
      <Calibration bins={report.calibration.bins} brier={report.calibration.brier_score} horizon={report.directional.horizon_minutes} minimum={report.minimum_sample} />
      <ForecastScoringPanel scoring={report.forecast_scoring} />
      <SignificancePanel significance={report.significance} horizon={report.directional.horizon_minutes} />
      <QualityTables report={report} />
      <RecentDecisions rows={report.recent} horizon={report.directional.horizon_minutes} />
    </>}
    {state && <MissedMoves report={state.missed ?? null} />}
    {state && <PostMortems items={state.postMortems} />}
    {report && <section className="panel" aria-label="Report limitations">
      <span className="eyebrow">REPORT LIMITATIONS</span>
      <h2>What this evidence cannot show</h2>
      {report.limitations.length ? <ul className="quality-limitations">{report.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="muted">The report declares no limitations. Treat that as a gap in the report, not as proof of completeness.</p>}
    </section>}
  </>;
}

/** The breakdowns. Each hit rate sits beside the decisions it was taken over, and one over
 * fewer than the report's minimum shows as a dash: a regime with ninety decisions and one
 * evaluated call has a count, not a rate. */
export function QualityTables({ report }: { report: DecisionQualityReport }) {
  return <>
    <p className="muted quality-note">A rate over fewer than {count(report.minimum_sample)} evaluated decisions shows as {DASH}: the count beside it is the finding.</p>
    <div className="quality-tables">
      <Panel eyebrow="OUTCOMES BY REGIME" title="By regime">
        <DataTable label="Decisions by regime" columns={[{ name: "Regime" }, { name: "Decisions", numeric: true }, { name: "Filled", numeric: true }, { name: "Evaluated", numeric: true }, { name: "Hit rate", numeric: true }, { name: "Net P&L", numeric: true }]}
          rows={report.by_regime.map((r) => [r.regime.replaceAll("_", " "), count(r.decisions), count(r.filled), count(r.evaluated), thinRate(r.hit_rate, r.evaluated, report.minimum_sample), <span className={signedClass(r.net_pnl)}>{signedMoney(r.net_pnl)}</span>])}
          empty="No regime breakdown in this report" />
      </Panel>
      <Panel eyebrow="OUTCOMES BY PLAYBOOK" title="By playbook">
        <DataTable label="Decisions by playbook" columns={[{ name: "Playbook" }, { name: "Decisions", numeric: true }, { name: "Filled", numeric: true }, { name: "Probes", numeric: true }, { name: "Evaluated", numeric: true }, { name: "Hit rate", numeric: true }, { name: "Net P&L", numeric: true }]}
          rows={report.by_playbook.map((r) => [r.playbook.replaceAll("_", " "), count(r.decisions), count(r.filled), count(r.probes), count(r.evaluated), thinRate(r.hit_rate, r.evaluated, report.minimum_sample), <span className={signedClass(r.net_pnl)}>{signedMoney(r.net_pnl)}</span>])}
          empty="No playbook breakdown in this report" />
      </Panel>
      <Panel eyebrow="OUTCOMES BY HOUR" title="By hour (IST)">
        <DataTable label="Decisions by hour IST" columns={[{ name: "Hour IST" }, { name: "Decisions", numeric: true }, { name: "Evaluated", numeric: true }, { name: "Hit rate", numeric: true }, { name: "Net P&L", numeric: true }]}
          rows={report.by_hour_ist.map((r) => [hourLabel(r.hour), count(r.decisions), count(r.evaluated), thinRate(r.hit_rate, r.evaluated, report.minimum_sample), <span className={signedClass(r.net_pnl)}>{signedMoney(r.net_pnl)}</span>])}
          empty="No hourly breakdown in this report" />
      </Panel>
      <Panel eyebrow="AGENT DIRECTION CALLS" title="By agent">
        <DataTable label="Directional accuracy by agent" columns={[{ name: "Agent" }, { name: "Evaluated", numeric: true }, { name: "Directional accuracy", numeric: true }]}
          rows={report.by_agent.map((r) => [r.agent_id, count(r.evaluated), thinRate(r.directional_accuracy, r.evaluated, report.minimum_sample)])}
          empty="No per-agent accuracy in this report" />
      </Panel>
      <Panel eyebrow="HOW TRADES ENDED" title="Exits">
        <DataTable label="Exit triggers" columns={[{ name: "Trigger" }, { name: "Closed trades", numeric: true }]}
          rows={report.trades.exits.map((r) => [r.trigger.replaceAll("_", " "), count(r.count)])}
          empty="No closed trades yet" />
      </Panel>
      <Panel eyebrow="WHY DECISIONS DID NOT FILL" title="Rejections">
        <DataTable label="Rejection reasons" columns={[{ name: "Reason" }, { name: "Decisions", numeric: true }]}
          rows={report.rejections.map((r) => [r.reason.replaceAll("_", " "), count(r.count)])}
          empty="No governance rejections recorded" />
      </Panel>
    </div>
  </>;
}

export function AiBudgetNotice({ budget }: { budget: AiBudget }) {
  const shared = budget.aggregate;
  const usage = shared ?? budget;
  const exhausted = budget.exhausted || usage.exhausted
    || budget.remaining_calls === 0 || budget.remaining_tokens === 0
    || usage.remaining_calls === 0 || usage.remaining_tokens === 0;
  const incomplete = !shared || usage.reserved_tokens === null;
  const tone = exhausted || incomplete ? "warning" : "muted";
  return <div className={`banner quality-budget ${tone}`} role="status">
    <strong>{exhausted ? "AI budget exhausted in this report" : incomplete ? "AI budget · incomplete shared usage" : "AI budget · shared usage"}</strong>
    <p>
      {`${count(usage.calls)} of ${count(budget.daily_call_limit)} ${shared ? "shared" : budget.scope} calls and ${count(usage.tokens)} of ${count(budget.daily_token_limit)} tokens used on ${budget.day} (UTC). `}
      {usage.reserved_tokens !== null ? `${count(usage.reserved_tokens)} tokens reserved for pending or unreported usage. ` : "Token reservations were not reported. "}
      {shared && `${count(Math.min(budget.remaining_calls, shared.remaining_calls))} calls and ${count(Math.min(budget.remaining_tokens, shared.remaining_tokens))} tokens remain for ${budget.scope} after shared limits. `}
      {exhausted
        ? "New model calls for this scope are blocked while the cap is exhausted. Budget-limited decisions do not measure strategy quality. Daily counters reset at 00:00 UTC."
        : shared ? "Each request must still fit its token reservation; headroom does not guarantee admission." : "Account-wide headroom is unknown; these scope counters cannot establish that Atlas can call the model."}
    </p>
    {shared && <p>{`${budget.scope}: ${count(budget.calls)} calls and ${count(budget.tokens)} tokens used. Other scopes share the same daily ceiling.`}</p>}
  </div>;
}

/** The rows of the hold split, in the order an operator acts on them: a deadlock and a
 * silence want opposite fixes, and a hard hold is not the lean's to fix at all. */
const HOLD_CAUSES: Array<{ key: Exclude<keyof HoldCauses, "holds">; label: string; meaning: string }> = [
  { key: "deadlock", label: "Deadlock", meaning: "Specialists leaned both ways and cancelled." },
  { key: "silent", label: "Silent", meaning: "No specialist leaned either way." },
  { key: "conviction_floor", label: "Conviction floor", meaning: "A one-sided lean under the entry score or the playbook floor, or held by the model. The journal does not say which." },
  { key: "hard_hold", label: "Hard hold", meaning: "Nothing was weighed: a veto, stale evidence or missing coverage. Never probed." },
  { key: "roster_unrecorded", label: "Roster not recorded", meaning: "A lean with no readable roster, so its shape is unknown." },
];

export function HoldCausesPanel({ holds, decisions }: { holds: HoldCauses | null; decisions: number }) {
  return <section className="panel" aria-label="Holds by cause">
    <div className="panel-title"><div><span className="eyebrow">WHY ATLAS HELD</span><h2>Holds by cause</h2></div>
      {holds && <span className="muted">{count(holds.holds)} of {count(decisions)} decisions held</span>}</div>
    {!holds
      ? <div className="empty">This report does not split its holds. A deadlock and a silent roster both hold, and they cannot be told apart here.</div>
      : <>
        <DataTable label="Holds by cause" columns={[{ name: "Cause" }, { name: "Holds", numeric: true }, { name: "What it means" }]}
          rows={HOLD_CAUSES.map((cause) => [<strong>{cause.label}</strong>, count(holds[cause.key]), <small>{cause.meaning}</small>])}
          empty="No holds in this window" />
        <p className="muted">Counts only, over this report&apos;s window. What the market did after each kind of hold is not scored on this page.</p>
      </>}
  </section>;
}

export function ModelDecisionHealth({ report }: { report: InferenceHealth | null }) {
  return <section className="panel" aria-label="Model decision health">
    <span className="eyebrow">RECORDED MODEL DIAGNOSTICS</span><h2>Model decision health</h2>
    {!report ? <div className="empty">No valid model diagnostic breakdown was recorded. Earlier failure causes cannot be reconstructed from decision totals.</div> : <>
      <p>{count(report.recorded)} of {count(report.decisions)} decisions carry diagnostics. {count(report.not_recorded)} have no recorded diagnostic.</p>
      <DataTable label="Model decision statuses" columns={[{ name: "Status" }, { name: "Decisions", numeric: true }]}
        rows={report.statuses.map((row) => [INFERENCE_STATUS_LABELS[row.status], count(row.decisions)])}
        empty="No model status has been recorded in this window." />
      <h3>Failure reasons</h3>
      <DataTable label="Model failure reasons" columns={[{ name: "Recorded cause" }, { name: "Decisions", numeric: true }]}
        rows={report.failures.map((row) => [row.code.replaceAll("_", " "), count(row.decisions)])}
        empty="No failure codes in the recorded diagnostics. Decisions without diagnostics remain unknown." />
    </>}
    <p className="muted">These are decision counts, not API request counts or provider error rates. A completed model response can still recommend waiting or be rejected by risk controls.</p>
  </section>;
}

export function VerdictBanner({ verdict }: { verdict: Verdict }) {
  const insufficient = verdict.state === "insufficient_sample";
  const tone = insufficient ? "warning" : verdict.state === "edge_candidate" ? "edge" : "pending";
  const title = insufficient ? "Insufficient sample" : verdict.state === "edge_candidate" ? "Edge candidate" : "No edge yet";
  return <div className={`banner quality-verdict ${tone}`} role="status" aria-live="polite" data-verdict={verdict.state}>
    <div>
      <span className="eyebrow">VERDICT · PAPER EVIDENCE ONLY</span>
      <strong>{title}{insufficient ? ` · ${count(verdict.evaluated)} of ${count(verdict.minimumSample)} directional decisions evaluated` : ""}</strong>
      <p>{verdict.sentence}</p>
      <div className="quality-checks" aria-label="Edge rule checks">
        <span className="pill neutral">Sample {count(verdict.evaluated)} / {count(verdict.minimumSample)}</span>
        {verdict.checks.map((c) => <span key={c.label} className={`pill ${insufficient ? "neutral" : c.pass ? "green" : "amber"}`}>{c.label}: {c.value}</span>)}
      </div>
    </div>
  </div>;
}

function Tile({ label, value, note, positive }: { label: string; value: string; note: string; positive?: boolean }) {
  return <div className="metric">
    <span>{label}</span>
    <strong className={positive === undefined ? "" : positive ? "positive" : "negative"}>{value}</strong>
    <p>{note}</p>
  </div>;
}

export function Headline({ report }: { report: DecisionQualityReport }) {
  const { counts, directional, trades, calibration } = report;
  const sign = (v: number | null) => v == null ? undefined : v >= 0;
  // One closed trade is a win rate of 0% or 100%. Below the report's own minimum the tile
  // shows the count and what it needs, never a rate.
  const minimum = report.minimum_sample;
  const calls = rateShown(directional.evaluated, minimum), closed = rateShown(trades.closed, minimum);
  return <div className="metric-grid" aria-label="Decision quality headline">
    <Tile label="Decisions" value={count(counts.decisions)} note={`${count(counts.filled)} filled · ${count(counts.rejected)} rejected · ${count(counts.abstained)} abstained`} />
    <Tile label="Exploration probes" value={count(counts.probes)} note={counts.probes == null
      ? "This report did not count probes; it cannot say how much of the sample was exploration."
      : `Directional decisions taken below the conviction floor · ${count(counts.decisions - counts.probes)} at or above it`} />
    <Tile label="Filled" value={count(counts.filled)} note={`${count(counts.closed_trades)} closed trades · ${count(counts.resolved_60m)} resolved at ${directional.horizon_minutes} min`} />
    <Tile label={`Hit rate ${directional.horizon_minutes}m`} value={thinRate(directional.hit_rate, directional.evaluated, minimum)} note={calls
      ? `${count(directional.evaluated)} evaluated · mean forward ${signedPercent(directional.mean_forward_return)}`
      : `${count(directional.evaluated)} evaluated · ${thinNote(directional.evaluated, minimum)}`} />
    <Tile label="Expectancy" value={thinRate(trades.expectancy, trades.closed, minimum, signedMoney)} positive={closed ? sign(trades.expectancy) : undefined} note={closed
      ? `Net P&L per closed trade · win rate ${percent(trades.win_rate)}`
      : `${count(trades.closed)} closed · ${thinNote(trades.closed, minimum)}`} />
    <Tile label="Profit factor" value={thinRate(trades.profit_factor, trades.closed, minimum, ratio)} note={closed
      ? `Gross wins ÷ gross losses · avg win ${money(trades.average_win)} · avg loss ${money(trades.average_loss)}`
      : `Gross wins ÷ gross losses · ${thinNote(trades.closed, minimum)}`} />
    <Tile label="Brier score" value={thinRate(calibration.brier_score, directional.evaluated, minimum, ratio)} note={calls
      ? "Lower is better · 0.25 is the coin-flip threshold"
      : `Lower is better · ${thinNote(directional.evaluated, minimum)}`} />
    <Tile label="Net P&L" value={signedMoney(trades.net_pnl)} positive={trades.net_pnl >= 0} note={`Gross ${signedMoney(trades.gross_pnl)} · fees ${money(trades.fees)} · avg hold ${minutes(trades.average_holding_minutes)}`} />
    <Tile label="Sessions" value={count(report.window.sessions)} note={`${day(report.window.since)} → ${day(report.window.until)} · minimum sample ${count(report.minimum_sample)}`} />
  </div>;
}

export function Calibration({ bins, brier, horizon, minimum }: { bins: CalibrationBin[]; brier: number | null; horizon: number; minimum: number }) {
  const width = (v: number) => `${Math.max(0, Math.min(100, v * 100))}%`;
  return <section className="panel" aria-label="Calibration">
    <div className="panel-title"><div><span className="eyebrow">CONFIDENCE VS OUTCOME</span><h2>Calibration</h2></div><span className="muted">Brier {rateShown(bins.reduce((sum, bin) => sum + bin.decisions, 0), minimum) ? ratio(brier) : DASH}</span></div>
    <p className="readiness-explanation">Each confidence bin compares what the agents claimed (mean confidence) with what happened (hit rate at {horizon} minutes). Bars of equal length are calibrated; a longer confidence bar is over-confidence. A bin with fewer than {count(minimum)} decisions shows no hit rate.</p>
    <div className="calibration-legend" aria-hidden="true"><span><i className="confidence" />Mean confidence</span><span><i className="hit" />Hit rate {horizon}m</span></div>
    {bins.length ? <div className="calibration-strip" role="list">{bins.map((bin) => {
      const quiet = bin.decisions === 0;
      const hit = thinRate(bin.hit_rate, bin.decisions, minimum);
      return <div className={`calibration-bin${quiet ? " quiet" : ""}`} role="listitem" key={`${bin.lower}-${bin.upper}`} aria-label={`Confidence ${bin.lower.toFixed(1)} to ${bin.upper.toFixed(1)}: mean confidence ${percent(bin.mean_confidence)}, hit rate ${hit}, ${count(bin.decisions)} decisions`}>
        <span className="numeric">{bin.lower.toFixed(1)}–{bin.upper.toFixed(1)}</span>
        <div>
          <div className="bar-track">{bin.mean_confidence != null && <span className="confidence" style={{ width: width(bin.mean_confidence) }} />}</div>
          <div className="bar-track">{bin.hit_rate != null && rateShown(bin.decisions, minimum) && <span className="hit" style={{ width: width(bin.hit_rate) }} />}</div>
          <small>{percent(bin.mean_confidence)} claimed · {hit} hit</small>
        </div>
        <span className="numeric calibration-count">{count(bin.decisions)}<small>decisions</small></span>
      </div>;
    })}</div> : <div className="empty">No calibration bins in this report</div>}
  </section>;
}

/**
 * The engine's scoring of the forecasts it stated before the outcomes existed. The
 * calibration strip above asks whether the agents' *confidence* tracked their hit rate;
 * this asks whether the *probability* each decision stated came true as often as it
 * claimed, against the cost and horizon stored with that decision.
 *
 * Bases are shown one below the other and never combined. A refit ships a new basis, and
 * one curve drawn over two mappings describes neither.
 */
export function ForecastScoringPanel({ scoring }: { scoring: ForecastScoring | null }) {
  return <section className="panel" aria-label="Forecast scoring">
    <div className="panel-title">
      <div><span className="eyebrow">STATED BEFORE THE OUTCOME</span><h2>Forecast scoring</h2></div>
      {scoring && <span className="muted">{count(scoring.with_forecast)} of {count(scoring.decisions)} decisions state a forecast</span>}
    </div>
    {!scoring
      ? <div className="empty">
          No readable forecast scoring in this report: either no forecast has been recorded over this window, or the
          block declares a schema this build does not read. Nothing about calibration is inferred from the confidence
          numbers above, which measure a different claim.
        </div>
      : <>
        <p className="readiness-explanation">
          Every decision states the probability that the forward return clears its own cost over its own horizon, written
          before the outcome exists. Brier and log score are proper: lower is better, and neither can be improved by
          hedging towards 0.5 or by exaggerating. Skill is measured against the base rate rather than a coin, so a
          forecast that has only learned how often the move happens scores zero here while still beating a coin.
        </p>
        <p className="readiness-explanation quality-note">
          {count(scoring.with_forecast)} forecasts recorded, {count(scoring.unscoreable.no_forecast)} decisions stated none.
          Not scored: {count(scoring.unscoreable.outcome_unresolved)} whose outcome has not resolved,{" "}
          {count(scoring.unscoreable.unknown_horizon)} naming a horizon with no resolver, and{" "}
          {count(scoring.unscoreable.invalid_probability)} with an unusable probability. Unresolved forecasts are excluded,
          never counted as misses, and no skill is claimed below {count(scoring.minimum_scored)} scored forecasts.
        </p>
        {scoring.by_basis.length
          ? scoring.by_basis.map((basis) => <ForecastBasis key={basis.basis} basis={basis} />)
          : <div className="empty">{scoring.with_forecast === 0
              ? "No decision in this window recorded a forecast, so there is nothing to score."
              : "Forecasts were recorded but none names the mapping that produced it, and a probability whose basis is unknown cannot be told apart from one written under a different mapping."}</div>}
        <h4 className="quality-subhead">What these scores cannot show</h4>
        {scoring.limitations.length
          ? <ul className="quality-limitations">{scoring.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>
          : <p className="muted">This scoring block declares no limitations. Treat that as a gap in the report, not as proof there are none.</p>}
      </>}
  </section>;
}

function ForecastBasis({ basis }: { basis: ForecastBasisRow }) {
  const short = basis.insufficient_sample;
  const width = (v: number) => `${Math.max(0, Math.min(100, v * 100))}%`;
  const { baselines: base, decomposition: parts } = basis;
  return <article className="forecast-basis" aria-label={`Forecast basis ${basis.basis}`}>
    <header>
      <strong>{basis.basis.replaceAll("_", " ")}</strong>
      <span className={`pill ${short ? "amber" : "neutral"}`}>{short ? "Insufficient sample" : `${count(basis.scored)} scored`}</span>
      <small>Base rate {percent(basis.base_rate)}</small>
    </header>
    {/* The engine's own sentence, verbatim: it states what this basis has and has not established. */}
    <p>{basis.verdict}</p>
    <DataTable label={`Scores for ${basis.basis}`}
      columns={[{ name: "Metric" }, { name: "These forecasts", numeric: true }, { name: "Base rate", numeric: true }, { name: "Coin flip", numeric: true }]}
      rows={[
        ["Brier score", ratio(basis.brier_score, 4), ratio(base?.base_rate.brier_score, 4), ratio(base?.coin_flip.brier_score, 4)],
        ["Log score", ratio(basis.log_score, 4), ratio(base?.base_rate.log_score, 4), ratio(base?.coin_flip.log_score, 4)],
        ["Skill against baseline", "—", signedRatio(basis.skill_vs_base_rate), signedRatio(basis.skill_vs_coin_flip)],
      ]}
      empty="No scores for this basis" />
    <p className="muted">
      The base-rate column always states {percent(base?.base_rate.probability)}, this sample&apos;s own frequency of the move
      clearing its cost. Skill is 1 when perfect, 0 when no better than that column, and negative when worse.
      {short && " This basis has too few scored forecasts for any of it to be a finding."}
    </p>
    {parts && <>
      <h4>Where the Brier score comes from</h4>
      <DataTable label={`Brier decomposition for ${basis.basis}`}
        columns={[{ name: "Term" }, { name: "Value", numeric: true }, { name: "What it says" }]}
        rows={[
          ["Reliability", ratio(parts.reliability, 6), "How far each bin sat from what that bin did. Lower is better; zero is perfect calibration."],
          ["Resolution", ratio(parts.resolution, 6), "How far the bins sat from the base rate. Higher is better; zero means the forecasts separated nothing."],
          ["Uncertainty", ratio(parts.uncertainty, 6), "Variance of the outcome itself, which no forecaster can change."],
          ["Within bin", signedRatio(parts.within_bin, 6), "The spread inside each bin that binning cannot explain. A large one means these bins are too coarse."],
          [<strong>Brier score</strong>, <strong>{ratio(basis.brier_score, 6)}</strong>, <span>reliability − resolution + uncertainty + within bin</span>],
        ]}
        empty="No decomposition for this basis" />
    </>}
    {basis.reliability.some((bin) => bin.forecasts > 0) ? <>
      <h4>Stated probability against what happened</h4>
      <div className="calibration-legend" aria-hidden="true"><span><i className="confidence" />Mean forecast</span><span><i className="hit" />Observed frequency</span></div>
      <div className="calibration-strip" role="list">{basis.reliability.map((bin) => {
        const quiet = bin.forecasts === 0;
        return <div className={`calibration-bin${quiet ? " quiet" : ""}`} role="listitem" key={`${bin.lower}-${bin.upper}`}
          aria-label={`Forecast ${bin.lower.toFixed(1)} to ${bin.upper.toFixed(1)}: mean forecast ${percent(bin.mean_forecast)}, observed ${percent(bin.observed_frequency)}, ${count(bin.forecasts)} forecasts`}>
          <span className="numeric">{bin.lower.toFixed(1)}–{bin.upper.toFixed(1)}</span>
          <div>
            <div className="bar-track">{bin.mean_forecast != null && <span className="confidence" style={{ width: width(bin.mean_forecast) }} />}</div>
            <div className="bar-track">{bin.observed_frequency != null && <span className="hit" style={{ width: width(bin.observed_frequency) }} />}</div>
            <small>{percent(bin.mean_forecast)} stated · {percent(bin.observed_frequency)} happened</small>
          </div>
          <span className="numeric calibration-count">{count(bin.forecasts)}<small>forecasts</small></span>
        </div>;
      })}</div>
    </> : <p className="muted">No forecast under this basis has resolved yet, so there is no reliability curve to draw.</p>}
  </article>;
}

/**
 * The engine's own t-statistics for the two means the page leads with. The report's
 * limitations already explain that these are uncorrected for multiple testing; until now
 * the page carried that caveat without ever showing the numbers it was about.
 */
function SignificancePanel({ significance, horizon }: { significance: Significance | null; horizon: number }) {
  const row = (label: string, block: TStatistic, unit: (v: number | null) => string) => [
    label, count(block.observations), unit(block.mean), unit(block.standard_error),
    <span className="numeric">{ratio(block.t_statistic)}</span>,
  ];
  return <section className="panel" aria-label="Statistical significance">
    <div className="panel-title"><div><span className="eyebrow">MEANS AGAINST ZERO</span><h2>Statistical significance</h2></div>
      {significance && <span className="muted">Multiple-testing correction: {significance.multiple_testing_correction}</span>}</div>
    {!significance
      ? <div className="empty">This report carries no t-statistics. A hit rate and an expectancy on their own do not say how far either mean is from zero.</div>
      : <>
        <p className="readiness-explanation">
          Each row tests one mean against zero on the observations shown. A t-statistic near zero is
          indistinguishable from no effect; the sign says which way. Both rows read as unavailable below{" "}
          {count(significance.minimum_observations)} observations or when the sample has no dispersion, because no
          t-statistic exists there.
        </p>
        <DataTable label="One-sample t-statistics" columns={[{ name: "Mean tested" }, { name: "Observations", numeric: true }, { name: "Mean", numeric: true }, { name: "Standard error", numeric: true }, { name: "t", numeric: true }]}
          rows={[
            row(`Forward return ${horizon}m`, significance.forward_return_60m, (v) => signedPercent(v, 4)),
            row("Closed-trade net P&L", significance.trade_net_pnl, signedMoney),
          ]}
          empty="No t-statistics in this report" />
      </>}
  </section>;
}

function Panel({ eyebrow, title, children }: { eyebrow: string; title: string; children: ReactNode }) {
  return <section className="panel"><span className="eyebrow">{eyebrow}</span><h3>{title}</h3>{children}</section>;
}

function DataTable({ label, columns, rows, empty }: { label: string; columns: Array<{ name: string; numeric?: boolean }>; rows: ReactNode[][]; empty: string }) {
  return <div className="table-scroll">
    <table aria-label={label}>
      <thead><tr>{columns.map((c) => <th key={c.name} className={c.numeric ? "numeric" : undefined}>{c.name}</th>)}</tr></thead>
      <tbody>{rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j} className={columns[j]?.numeric ? "numeric" : undefined}>{cell}</td>)}</tr>)}</tbody>
    </table>
    {!rows.length && <div className="empty">{empty}</div>}
  </div>;
}

function RecentDecisions({ rows, horizon }: { rows: RecentDecision[]; horizon: number }) {
  const tone = (g: RecentDecision["governance"]) => g === "filled" ? "green" : g === "rejected" ? "amber" : "neutral";
  return <section className="panel" aria-label="Recent decisions">
    <div className="panel-title"><div><span className="eyebrow">NEWEST FIRST</span><h2>Recent decisions</h2></div><span className="muted">{count(rows.length)} shown</span></div>
    <div className="table-scroll">
      <table aria-label="Recent decisions">
        <thead><tr><th>Decided</th><th>Instrument</th><th>Stance</th><th className="numeric">Confidence</th><th>Regime</th><th>Playbook</th><th>Mode</th><th>Conviction</th><th>Governance</th><th>Reason</th><th className="numeric">Fwd {horizon}m</th><th className="numeric">Net P&L</th><th>Exit</th></tr></thead>
        <tbody>{rows.map((d) => <tr key={d.decision_id}>
          <td><small>{when(d.decided_at)}</small></td>
          <td><strong>{d.symbol}</strong></td>
          <td>{d.stance}</td>
          <td className="numeric">{percent(d.confidence, 0)}</td>
          <td>{d.regime?.replaceAll("_", " ") ?? "—"}</td>
          <td>{d.playbook?.replaceAll("_", " ") ?? "—"}</td>
          <td>{d.mode ?? "—"}</td>
          <td>{d.probe == null ? <span className="muted">not recorded</span> : d.probe ? <span className="pill amber">Probe</span> : "Conviction"}</td>
          <td><span className={`pill ${tone(d.governance)}`}>{d.governance.toUpperCase()}</span></td>
          <td>{d.reason?.replaceAll("_", " ") ?? "—"}</td>
          <td className={`numeric ${signedClass(d.forward_return_60m)}`}>{signedPercent(d.forward_return_60m)}</td>
          <td className={`numeric ${signedClass(d.net_pnl)}`}>{signedMoney(d.net_pnl)}</td>
          <td>{d.exit_trigger?.replaceAll("_", " ") ?? "—"}</td>
        </tr>)}</tbody>
      </table>
      {!rows.length && <div className="empty">No decisions in this window</div>}
    </div>
  </section>;
}

function MissedMoves({ report }: { report: MissedOpportunities | null }) {
  // On 21 September 2026 every decision was a hold, and this page could only say the
  // sample was insufficient. The holds are scored here: an up-move past the threshold
  // after a hold is a missed move, a down-move of the same size an avoided one.
  const clock = (value: string) => Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false })
    : "—";
  return <section className="panel" aria-label="Missed moves">
    <div className="panel-title">
      <div><span className="eyebrow">WHAT THE HOLDS LET GO BY</span><h2>Missed moves</h2></div>
      {report && <span className="muted">Session {report.session_date} · threshold {signedPercent(report.threshold)} on {report.horizon.replace("forward_return_", "")}</span>}
    </div>
    {report ? <>
      <p className="readiness-explanation quality-note">
        {count(report.holds)} holds, {count(report.evaluated)} evaluated: {count(report.missed)} missed, {count(report.avoided)} avoided, {count(report.unresolved)} unresolved.
        Measured on the feed&apos;s mark at the horizon, before costs; evidence of what the swarm saw, not a trade that would have filled.
      </p>
      <DataTable label="Missed moves by instrument"
        columns={[{ name: "Instrument" }, { name: "Missed", numeric: true }, { name: "Avoided", numeric: true }, { name: "Evaluated", numeric: true }, { name: "Best missed move" }]}
        rows={report.symbols.map((s) => [
          <strong>{s.symbol}</strong>, count(s.missed), count(s.avoided), count(s.evaluated),
          s.best
            ? <span><span className="positive">{signedPercent(s.best.forward_return)}</span> at {clock(s.best.decided_at)} IST · {s.best.regime?.replaceAll("_", " ") ?? "regime unknown"} · {stancesSummary(s.best.agents)}</span>
            : "—",
        ])}
        empty="No holds journaled for this session" />
    </> : <div className="empty">No missed-move file yet; the engine writes one per session when PRAMANA_MISSED_OPPORTUNITY_DIR is set.</div>}
  </section>;
}

export function PostMortems({ items }: { items: PostMortem[] }) {
  // Every section the engine writes is shown. The counts, rejection reasons and regime
  // split are what explain a session, and a no-trade session is explained only by those.
  const COUNT_ORDER = ["decisions", "filled", "rejected", "abstained", "probes", "closed_trades", "resolved_60m"];
  const ordered = (counts: Record<string, number>) => {
    const keys = Object.keys(counts);
    return [...COUNT_ORDER.filter((key) => key in counts), ...keys.filter((key) => !COUNT_ORDER.includes(key))];
  };
  // A session's hit rate is shown only over RATE_MINIMUM evaluated decisions; a post-mortem
  // written before it recorded the count shows none.
  const rate = (value: number | null, evaluated: number | null) => !rateShown(evaluated, RATE_MINIMUM)
    ? `${DASH} (${evaluated == null ? "sample not recorded" : `${count(evaluated)} evaluated`})`
    : value === null ? "Unavailable" : percent(value);
  return <section className="panel" aria-label="Session post-mortems">
    <div className="panel-title"><div><span className="eyebrow">OPERATOR REVIEW</span><h2>Session post-mortems</h2></div>{items.length > 0 && <span className="muted">Latest {count(items.length)}</span>}</div>
    <p className="readiness-explanation quality-note">Approval happens on the host with <code>pramana post-mortem --approve &lt;date&gt;</code>. This dashboard reads the files; it does not approve them.</p>
    {items.length ? items.map((pm) => <article className="post-mortem" key={pm.session_date} aria-label={`Post-mortem ${pm.session_date}`}>
      <header>
        <strong>{pm.session_date}</strong>
        <span className={`pill ${pm.status === "approved" ? "green" : "amber"}`}>{pm.status === "approved" ? "APPROVED" : "PENDING"}</span>
        <small>Generated {when(pm.generated_at)}{pm.approved_at ? ` \u00b7 approved ${when(pm.approved_at)}` : ""} \u00b7 {pm.tenant_id}</small>
      </header>
      <dl className="details post-mortem-summary">
        {ordered(pm.summary.counts).map((key) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{count(pm.summary.counts[key])}</dd></div>)}
        <div><dt>net p&amp;l</dt><dd className={signedClass(pm.summary.net_pnl)}>{signedMoney(pm.summary.net_pnl)}</dd></div>
        <div><dt>hit rate 60m</dt><dd>{rate(pm.summary.hit_rate_60m, pm.summary.evaluated_60m)}</dd></div>
      </dl>
      <h4>Why decisions did not become trades</h4>
      {pm.summary.rejections.length
        ? <div className="research-table-scroll" role="region" tabIndex={0}><table><thead><tr><th scope="col">Rejection reason</th><th scope="col">Decisions</th></tr></thead>
            <tbody>{pm.summary.rejections.map((item) => <tr key={item.reason}><td>{item.reason.replaceAll("_", " ")}</td><td>{count(item.count)}</td></tr>)}</tbody></table></div>
        : <p className="muted">No governed rejection in this session. A session with no fills and no rejection abstained at the consensus, not at a gate.</p>}
      <h4>By regime</h4>
      {pm.summary.by_regime.length
        ? <div className="research-table-scroll" role="region" tabIndex={0}><table><thead><tr><th scope="col">Regime</th><th scope="col">Decisions</th><th scope="col">Filled</th><th scope="col">Hit rate</th><th scope="col">Net P&amp;L</th></tr></thead>
            <tbody>{pm.summary.by_regime.map((item) => <tr key={item.regime}><td>{item.regime.replaceAll("_", " ")}</td><td>{count(item.decisions)}</td><td>{count(item.filled)}</td><td>{rate(item.hit_rate, item.evaluated)}</td><td className={signedClass(item.net_pnl)}>{signedMoney(item.net_pnl)}</td></tr>)}</tbody></table></div>
        : <p className="muted">No regime was recorded for this session.</p>}
      <details><summary>Exit triggers ({count(pm.summary.exits.length)})</summary>
        {pm.summary.exits.length
          ? <ul>{pm.summary.exits.map((item) => <li key={item.trigger}>{item.trigger.replaceAll("_", " ")} \u00b7 {count(item.count)}</li>)}</ul>
          : <p className="muted">Nothing was exited in this session.</p>}
      </details>
      <details><summary>Hourly results ({count(pm.summary.by_hour_ist.length)})</summary>
        {pm.summary.by_hour_ist.length
          ? <div className="research-table-scroll" role="region" tabIndex={0}><table><thead><tr><th scope="col">Hour IST</th><th scope="col">Decisions</th><th scope="col">Hit rate</th><th scope="col">Net P&amp;L</th></tr></thead>
              <tbody>{pm.summary.by_hour_ist.map((item) => <tr key={item.hour}><td>{hourLabel(item.hour)}</td><td>{count(item.decisions)}</td><td>{rate(item.hit_rate, item.evaluated)}</td><td className={signedClass(item.net_pnl)}>{signedMoney(item.net_pnl)}</td></tr>)}</tbody></table></div>
          : <p className="muted">No hourly breakdown was recorded.</p>}
      </details>
      {pm.lessons.length ? <ul>{pm.lessons.map((lesson, i) => <li key={i}>{lesson}</li>)}</ul> : <p className="muted">No lessons recorded for this session.</p>}
    </article>) : <div className="empty">No session post-mortems yet; the engine writes one per completed session.</div>}
  </section>;
}
