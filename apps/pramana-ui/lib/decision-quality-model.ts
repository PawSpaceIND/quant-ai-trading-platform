/**
 * Pure model for the engine's decision-quality report and session post-mortems:
 * types, bounded validation, formatting and the edge verdict. No file access,
 * so the client panel, the API route and the tests share one definition.
 */
export const DECISION_QUALITY_SCHEMA = "pramana.decision_quality.v1";
export const POST_MORTEM_SCHEMA = "pramana.post_mortem.v1";
export const REPORT_LIMIT_BYTES = 4 * 1024 * 1024;
export const POST_MORTEM_LIMIT_BYTES = 512 * 1024;
export const POST_MORTEM_FILE_LIMIT = 30;
export const LESSON_LIMIT = 200;

/** Edge rule thresholds. The verdict sentence states them so the reader never has to guess. */
export const EDGE_RULE = { hitRate: 0.5, expectancy: 0, profitFactor: 1, brier: 0.25 } as const;

export type Governance = "filled" | "rejected" | "abstained";
export type CountRow = { reason: string; count: number };
export type ExitRow = { trigger: string; count: number };
export type CalibrationBin = { lower: number; upper: number; decisions: number; hit_rate: number | null; mean_confidence: number | null };
export type RegimeRow = { regime: string; decisions: number; filled: number; hit_rate: number | null; net_pnl: number };
export type PlaybookRow = { playbook: string; decisions: number; filled: number; probes: number | null; hit_rate: number | null; net_pnl: number };
/** One-sample t-statistic for a mean against zero. Null throughout when the sample is
 * too small (see `minimum_observations`) or has no dispersion. */
export type TStatistic = { observations: number; mean: number | null; standard_error: number | null; t_statistic: number | null };
export type Significance = {
  minimum_observations: number;
  /** The engine states its own correction, or "none". Rendered verbatim, never assumed. */
  multiple_testing_correction: string;
  forward_return_60m: TStatistic;
  trade_net_pnl: TStatistic;
};
export type HourRow = { hour: number; decisions: number; hit_rate: number | null; net_pnl: number };
export type AgentRow = { agent_id: string; evaluated: number; directional_accuracy: number | null };
export type ModeRow = { mode: string; decisions: number };
export const INFERENCE_STATUS_LABELS = {
  completed: "Model response completed", invalid_schema: "Response failed validation",
  unavailable: "Provider unavailable", budget_exhausted: "Budget blocked",
  unverified: "Model evidence unverified", not_requested: "No model requested",
} as const;
export type InferenceHealth = {
  decisions: number; recorded: number; not_recorded: number;
  statuses: Array<{ status: keyof typeof INFERENCE_STATUS_LABELS; decisions: number }>;
  failures: Array<{ code: string; decisions: number }>;
};
/** The engine's own scoring of the directional forecasts it recorded. Its schema is
 * declared separately from the report's because a refit of the scoring rules ships a new
 * one, and numbers written under a different version must never be rendered under these
 * labels. */
export const FORECAST_SCORING_SCHEMA = "pramana.forecast_scoring.v1";
export type ForecastBaseline = { brier_score: number | null; log_score: number | null };
export type ForecastBaselines = {
  /** Always stating 0.5. The weaker claim, shown because the gap between the two is the point. */
  coin_flip: ForecastBaseline;
  /** Always stating how often the move happens. The baseline skill is measured against. */
  base_rate: ForecastBaseline & { probability: number | null };
};
/** Murphy's decomposition plus `within_bin`, the part binning cannot explain. The four
 * terms reconstruct the Brier score exactly as published. */
export type ForecastDecomposition = { reliability: number; resolution: number; uncertainty: number; within_bin: number };
export type ForecastReliabilityBin = { lower: number; upper: number; forecasts: number; mean_forecast: number | null; observed_frequency: number | null };
export type ForecastBasisRow = {
  /** The named mapping these forecasts were written under. Bases are never pooled. */
  basis: string;
  scored: number;
  insufficient_sample: boolean;
  base_rate: number | null;
  brier_score: number | null;
  log_score: number | null;
  baselines: ForecastBaselines | null;
  /** 1 is perfect, 0 is no better than the baseline, negative is worse. Null when the
   * baseline leaves no room to improve on. */
  skill_vs_base_rate: number | null;
  skill_vs_coin_flip: number | null;
  decomposition: ForecastDecomposition | null;
  reliability: ForecastReliabilityBin[];
  /** The engine's own sentence about what this basis has established. Rendered verbatim. */
  verdict: string;
};
export type ForecastUnscoreable = { no_forecast: number; unknown_horizon: number; outcome_unresolved: number; invalid_probability: number };
export type ForecastScoring = {
  schema: typeof FORECAST_SCORING_SCHEMA;
  minimum_scored: number;
  decisions: number;
  with_forecast: number;
  unscoreable: ForecastUnscoreable;
  by_basis: ForecastBasisRow[];
  limitations: string[];
};

export type RecentDecision = {
  decision_id: string; decided_at: string; symbol: string; stance: string; confidence: number;
  regime: string | null; mode: string | null; governance: Governance; reason: string | null; order_id: string | null;
  forward_return_60m: number | null; net_pnl: number | null; exit_trigger: string | null;
  /** Exploration probe below the conviction floor. Null on a report written before the
   * engine recorded it, which is not the same claim as false. */
  probe: boolean | null;
  playbook: string | null;
};
export type DecisionQualityReport = {
  schema: typeof DECISION_QUALITY_SCHEMA;
  tenant_id: string;
  generated_at: string;
  window: { since: string; until: string; sessions: number };
  minimum_sample: number;
  insufficient_sample: boolean;
  /** `probes` is null on a report written before the engine counted them; a probe count
   * of zero is a different claim from not having counted. */
  counts: { decisions: number; filled: number; rejected: number; abstained: number; resolved_60m: number; closed_trades: number; probes: number | null };
  rejections: CountRow[];
  directional: { horizon_minutes: number; evaluated: number; hit_rate: number | null; mean_forward_return: number | null };
  trades: {
    closed: number; win_rate: number | null; expectancy: number | null; profit_factor: number | null;
    average_win: number | null; average_loss: number | null; average_holding_minutes: number | null;
    net_pnl: number; gross_pnl: number; fees: number; exits: ExitRow[];
  };
  calibration: { brier_score: number | null; bins: CalibrationBin[] };
  /** Null on a report written before the engine computed t-statistics. The limitations
   * list explains what they do and do not establish. */
  significance: Significance | null;
  by_regime: RegimeRow[];
  by_playbook: PlaybookRow[];
  by_hour_ist: HourRow[];
  by_agent: AgentRow[];
  by_mode: ModeRow[];
  recent: RecentDecision[];
  limitations: string[];
  /** Today's consensus spend headroom. Absent when the engine runs without a budget. */
  ai_budget: AiBudget | null;
  inference_health: InferenceHealth | null;
  /** Null on a report written before the engine scored forecasts, and on a block whose
   * schema is not the one above. */
  forecast_scoring: ForecastScoring | null;
};

export type AiBudgetUsage = {
  calls: number; tokens: number; reserved_tokens: number | null;
  remaining_calls: number; remaining_tokens: number; exhausted: boolean;
};
export type AiBudget = AiBudgetUsage & {
  day: string; scope: string;
  daily_call_limit: number; daily_token_limit: number;
  /** Shared ledger across scopes; null for older or malformed reports. */
  aggregate: AiBudgetUsage | null;
};

export type PostMortemStatus = "pending" | "approved";
/** Named tallies as the engine wrote them. Parsed as an open record of finite numbers so
 * a report written before a counter existed still reads, rather than being rejected. */
export type PostMortemCounts = Record<string, number>;
export type PostMortemExit = {trigger: string; count: number};
export type PostMortemRejection = {reason: string; count: number};
export type PostMortemRegime = {regime: string; decisions: number; filled: number; hit_rate: number | null; net_pnl: number};
export type PostMortemHour = {hour: number; decisions: number; hit_rate: number | null; net_pnl: number};
export type PostMortemSummary = {
  counts: PostMortemCounts;
  net_pnl: number;
  hit_rate_60m: number | null;
  exits: PostMortemExit[];
  rejections: PostMortemRejection[];
  by_regime: PostMortemRegime[];
  by_hour_ist: PostMortemHour[];
};
export type PostMortem = {
  schema: typeof POST_MORTEM_SCHEMA;
  tenant_id: string;
  session_date: string;
  generated_at: string;
  status: PostMortemStatus;
  approved_at: string | null;
  /** The engine's own session summary. Every section it writes is carried, because
   * dropping the counts, rejection reasons and regime split leaves a post-mortem that
   * cannot explain the session it reviews. */
  summary: PostMortemSummary;
  /** Operator-reviewed plain text, each entry at most LESSON_LIMIT characters. */
  lessons: string[];
};

export type VerdictState = "insufficient_sample" | "no_edge_yet" | "edge_candidate";
export type VerdictCheck = { label: string; value: string; pass: boolean };
export type Verdict = { state: VerdictState; sentence: string; evaluated: number; minimumSample: number; checks: VerdictCheck[] };

/* ---------- formatting helpers shared by the panel, the verdict and the tests ---------- */

export const DASH = "—";
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const whole = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const present = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** Fraction (0.53) rendered as a percentage, matching the workspace's two-decimal style. */
export const percent = (v: number | null | undefined, digits = 2) => present(v) ? `${(v * 100).toFixed(digits)}%` : DASH;
/** Fraction rendered with an explicit sign, for forward returns. */
export const signedPercent = (v: number | null | undefined, digits = 2) => present(v) ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%` : DASH;
/** Unit-free ratio such as a profit factor or a Brier score. */
export const ratio = (v: number | null | undefined, digits = 2) => present(v) ? v.toFixed(digits) : DASH;
/** Unit-free ratio with an explicit sign, for skill scores where the sign is the finding:
 * positive beats the baseline, negative is worse than it. */
export const signedRatio = (v: number | null | undefined, digits = 3) => present(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(digits)}` : DASH;
/** Account-currency amount in the workspace's en-IN style. */
export const money = (v: number | null | undefined) => present(v) ? inr.format(v) : DASH;
/** Amount with an explicit sign, for P&L columns. */
export const signedMoney = (v: number | null | undefined) => present(v) ? `${v >= 0 ? "+" : ""}${inr.format(v)}` : DASH;
export const count = (v: number | null | undefined) => present(v) ? whole.format(v) : DASH;
export const minutes = (v: number | null | undefined) => present(v) ? `${v.toFixed(0)} min` : DASH;
/** Hour-of-day bucket label; the report already expresses hours in IST. */
export const hourLabel = (hour: number) => {
  const pad = (h: number) => String(((h % 24) + 24) % 24).padStart(2, "0");
  return `${pad(hour)}:00–${pad(hour + 1)}:00`;
};

/** Lessons are plain text: control characters and line breaks collapse to spaces and the result is capped at LESSON_LIMIT characters. */
export function truncateLesson(text: string, limit = LESSON_LIMIT): string {
  const visible = Array.from(text).map((ch) => { const code = ch.codePointAt(0) ?? 0; return code < 32 || code === 127 ? " " : ch; }).join("");
  const clean = Array.from(visible.replace(/\s+/g, " ").trim());
  return clean.length > limit ? `${clean.slice(0, limit - 1).join("")}…` : clean.join("");
}

/* ---------- verdict ---------- */

/**
 * One of three honest states. The rule: evaluated >= minimum_sample, hit rate > 0.5,
 * expectancy > 0, profit factor > 1 and Brier score < 0.25. A null metric cannot
 * pass. The sentence repeats the rule and the values that decided it.
 */
export function verdictFor(report: DecisionQualityReport): Verdict {
  const evaluated = report.directional.evaluated;
  const minimumSample = report.minimum_sample;
  const { hit_rate } = report.directional;
  const { expectancy, profit_factor } = report.trades;
  const brier = report.calibration.brier_score;
  const checks: VerdictCheck[] = [
    { label: "Hit rate 60m", value: `${percent(hit_rate)} vs > ${percent(EDGE_RULE.hitRate, 0)}`, pass: present(hit_rate) && hit_rate > EDGE_RULE.hitRate },
    { label: "Expectancy", value: `${signedMoney(expectancy)} vs > ${EDGE_RULE.expectancy}`, pass: present(expectancy) && expectancy > EDGE_RULE.expectancy },
    { label: "Profit factor", value: `${ratio(profit_factor)} vs > ${EDGE_RULE.profitFactor}`, pass: present(profit_factor) && profit_factor > EDGE_RULE.profitFactor },
    { label: "Brier score", value: `${ratio(brier)} vs < ${EDGE_RULE.brier}`, pass: present(brier) && brier < EDGE_RULE.brier },
  ];
  const rule = `hit rate above ${percent(EDGE_RULE.hitRate, 0)}, expectancy above ${EDGE_RULE.expectancy}, profit factor above ${EDGE_RULE.profitFactor} and Brier score below ${EDGE_RULE.brier}`;
  const short = evaluated < minimumSample;
  if (short || report.insufficient_sample) {
    const why = short
      ? `The edge rule (${rule}) is only applied once at least ${count(minimumSample)} decisions have a ${report.directional.horizon_minutes}-minute outcome.`
      : `The report itself marks the sample as insufficient, so the edge rule (${rule}) is not applied.`;
    return {
      state: "insufficient_sample", evaluated, minimumSample, checks,
      sentence: `${count(evaluated)} of ${count(minimumSample)} directional decisions evaluated; do not read these numbers as edge yet. ${why}`,
    };
  }
  const failing = checks.filter((c) => !c.pass);
  if (failing.length) {
    const named = failing.map((c) => { const shown = c.value.split(" vs ")[0]; return `${c.label.toLowerCase()} ${shown === DASH ? "unavailable" : shown}`; }).join(", ");
    return {
      state: "no_edge_yet", evaluated, minimumSample, checks,
      sentence: `No edge yet: ${count(evaluated)} evaluated decisions meet the minimum of ${count(minimumSample)}, but the rule needs ${rule} all at once. Not met: ${named}.`,
    };
  }
  return {
    state: "edge_candidate", evaluated, minimumSample, checks,
    sentence: `Edge candidate: ${count(evaluated)} evaluated decisions (minimum ${count(minimumSample)}) clear every part of the rule, ${rule}: hit rate ${percent(hit_rate)}, expectancy ${signedMoney(expectancy)}, profit factor ${ratio(profit_factor)}, Brier score ${ratio(brier)}. This is paper evidence over a short window, not a live-trading approval.`,
  };
}

/* ---------- validation ---------- */

class InvalidEvidence extends Error {}
const fail = (): never => { throw new InvalidEvidence("invalid evidence"); };
const record = (v: unknown): Record<string, unknown> => v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : fail();
const list = (v: unknown, max: number): unknown[] => Array.isArray(v) ? v.slice(0, max) : fail();
const num = (v: unknown): number => typeof v === "number" && Number.isFinite(v) ? v : fail();
const optNum = (v: unknown): number | null => v === null ? null : num(v);
const bool = (v: unknown): boolean => typeof v === "boolean" ? v : fail();
const text = (v: unknown, max = 200): string => typeof v === "string" ? v.slice(0, max) : fail();
const optText = (v: unknown, max = 200): string | null => v === null ? null : text(v, max);
const stamp = (v: unknown): string => { const s = text(v, 80); return Number.isFinite(Date.parse(s)) ? s : fail(); };
const governance = (v: unknown): Governance => v === "filled" || v === "rejected" || v === "abstained" ? v : fail();

function budgetUsage(value: unknown): AiBudgetUsage {
  const d = record(value);
  const counter = (v: unknown) => {
    const n = num(v);
    return Number.isSafeInteger(n) && n >= 0 ? n : fail();
  };
  return {
    calls: counter(d.calls), tokens: counter(d.tokens),
    reserved_tokens: d.reserved_tokens == null ? null : counter(d.reserved_tokens),
    remaining_calls: counter(d.remaining_calls), remaining_tokens: counter(d.remaining_tokens),
    exhausted: bool(d.exhausted),
  };
}

function optAiBudget(value: unknown): AiBudget | null {
  // Absent on an engine without a budget, and older engines never wrote it at all, so a
  // missing or malformed block yields null instead of failing the whole report.
  if (value === null || value === undefined) return null;
  try {
    const d = record(value);
    let aggregate: AiBudgetUsage | null = null;
    try { aggregate = budgetUsage(d.aggregate); } catch { /* Scope evidence remains usable. */ }
    return {
      ...budgetUsage(d),
      day: text(d.day, 20), scope: text(d.scope, 40),
      daily_call_limit: num(d.daily_call_limit), daily_token_limit: num(d.daily_token_limit),
      aggregate,
    };
  } catch {
    return null;
  }
}

function optInferenceHealth(value: unknown, expectedDecisions: number): InferenceHealth | null {
  if (value == null) return null;
  try {
    const d = record(value);
    const counter = (v: unknown) => { const n = num(v); return Number.isSafeInteger(n) && n >= 0 ? n : fail(); };
    const decisions = counter(d.decisions), recorded = counter(d.recorded), not_recorded = counter(d.not_recorded);
    const statuses = list(d.statuses, 6).map((value) => {
      const row = record(value), status = text(row.status, 40);
      if (!Object.hasOwn(INFERENCE_STATUS_LABELS, status)) fail();
      return { status: status as keyof typeof INFERENCE_STATUS_LABELS, decisions: counter(row.decisions) };
    });
    const allowed = new Set([
      "provider_timeout", "provider_overloaded", "provider_auth", "provider_rate_limited", "provider_unavailable",
      "budget_exhausted", "unverified_inference", "output_truncated", "model_refusal", "context_limit", "incomplete_turn",
      "completion_unverified", "missing_consensus_tool", "multiple_tool_blocks", "unexpected_tool", "tool_input_not_object",
      "missing_consensus_fields", "unknown_consensus_fields", "confidence_out_of_range", "negative_expected_risk",
      "stance_not_string", "unsupported_stance", "rationale_empty", "proof_not_object", "proof_fields_invalid",
      "proof_summary_empty", "invalid_consensus_schema",
      ...["confidence", "expected_return", "expected_risk"].flatMap((f) => [`${f}_not_numeric`, `${f}_nonfinite`]),
      ...["rationale", "supporting_factors", "risk_factors"].map((f) => `${f}_not_string_array`),
    ]);
    const failures = list(d.failures, 40).map((value) => {
      const row = record(value), code = text(row.code, 80);
      return { code: allowed.has(code) ? code : "unclassified_failure", decisions: counter(row.decisions) };
    });
    if (decisions !== expectedDecisions || recorded + not_recorded !== decisions
      || statuses.reduce((sum, row) => sum + row.decisions, 0) !== recorded
      || new Set(statuses.map((row) => row.status)).size !== statuses.length
      || failures.reduce((sum, row) => sum + row.decisions, 0) > recorded) fail();
    return { decisions, recorded, not_recorded, statuses, failures };
  } catch { return null; }
}

/**
 * The engine's forecast scoring, or null. The block declares its own schema and one
 * declaring a different version is dropped rather than rendered: a refit changes what the
 * numbers mean, and printing them under these labels is the same error as pooling two
 * bases into one curve.
 */
function optForecastScoring(value: unknown, expectedDecisions: number): ForecastScoring | null {
  if (value == null) return null;
  try {
    const d = record(value);
    if (d.schema !== FORECAST_SCORING_SCHEMA) fail();
    const counter = (v: unknown) => { const n = num(v); return Number.isSafeInteger(n) && n >= 0 ? n : fail(); };
    const decisions = counter(d.decisions), with_forecast = counter(d.with_forecast);
    const u = record(d.unscoreable);
    const unscoreable: ForecastUnscoreable = {
      no_forecast: counter(u.no_forecast), unknown_horizon: counter(u.unknown_horizon),
      outcome_unresolved: counter(u.outcome_unresolved), invalid_probability: counter(u.invalid_probability),
    };
    const baselines = (raw: unknown): ForecastBaselines | null => {
      if (raw == null) return null;
      const b = record(raw), coin = record(b.coin_flip), base = record(b.base_rate);
      return {
        coin_flip: { brier_score: optNum(coin.brier_score), log_score: optNum(coin.log_score) },
        base_rate: { probability: optNum(base.probability), brier_score: optNum(base.brier_score), log_score: optNum(base.log_score) },
      };
    };
    const decomposition = (raw: unknown): ForecastDecomposition | null => {
      if (raw == null) return null;
      const c = record(raw);
      return { reliability: num(c.reliability), resolution: num(c.resolution), uncertainty: num(c.uncertainty), within_bin: num(c.within_bin) };
    };
    const by_basis: ForecastBasisRow[] = list(d.by_basis, 50).map((item) => {
      const b = record(item);
      return {
        basis: text(b.basis, 80), scored: counter(b.scored), insufficient_sample: bool(b.insufficient_sample),
        base_rate: optNum(b.base_rate), brier_score: optNum(b.brier_score), log_score: optNum(b.log_score),
        baselines: baselines(b.baselines),
        skill_vs_base_rate: optNum(b.skill_vs_base_rate), skill_vs_coin_flip: optNum(b.skill_vs_coin_flip),
        decomposition: decomposition(b.decomposition),
        reliability: list(b.reliability, 20).map((bin) => {
          const r = record(bin);
          return {
            lower: num(r.lower), upper: num(r.upper), forecasts: counter(r.forecasts),
            mean_forecast: optNum(r.mean_forecast), observed_frequency: optNum(r.observed_frequency),
          };
        }),
        verdict: text(b.verdict, 500),
      };
    });
    // A decision either states a forecast or it does not, so those two counts partition
    // the report's decisions. A block failing that is describing a different set of rows
    // than the report around it, and showing it beside those counts would misdescribe the
    // sample. Two rows under one basis would be two curves for one mapping.
    if (decisions !== expectedDecisions
      || unscoreable.no_forecast + with_forecast !== decisions
      || by_basis.reduce((sum, row) => sum + row.scored, 0) > with_forecast
      || new Set(by_basis.map((row) => row.basis)).size !== by_basis.length) fail();
    return {
      schema: FORECAST_SCORING_SCHEMA, minimum_scored: counter(d.minimum_scored),
      decisions, with_forecast, unscoreable, by_basis,
      limitations: list(d.limitations, 20).map((item) => text(item, 500)),
    };
  } catch {
    return null;
  }
}

/** The engine's t-statistics, or null. Every field of a block is null together when the
 * sample is too small or has no dispersion, so a partial block is treated as no block. */
function optSignificance(value: unknown): Significance | null {
  if (value == null) return null;
  try {
    const d = record(value);
    const block = (raw: unknown): TStatistic => {
      const b = record(raw);
      const observations = num(b.observations);
      if (!Number.isSafeInteger(observations) || observations < 0) fail();
      return { observations, mean: optNum(b.mean), standard_error: optNum(b.standard_error), t_statistic: optNum(b.t_statistic) };
    };
    return {
      minimum_observations: num(d.minimum_observations),
      multiple_testing_correction: text(d.multiple_testing_correction, 80),
      forward_return_60m: block(d.forward_return_60m),
      trade_net_pnl: block(d.trade_net_pnl),
    };
  } catch {
    return null;
  }
}

function normalizeReport(value: unknown): DecisionQualityReport {
  const r = record(value);
  if (r.schema !== DECISION_QUALITY_SCHEMA) fail();
  const window = record(r.window), counts = record(r.counts), directional = record(r.directional);
  const trades = record(r.trades), calibration = record(r.calibration);
  const recent = list(r.recent, 200).map((item) => {
    const d = record(item);
    return {
      decision_id: text(d.decision_id, 120), decided_at: stamp(d.decided_at), symbol: text(d.symbol, 40), stance: text(d.stance, 40),
      confidence: num(d.confidence), regime: optText(d.regime, 60), mode: optText(d.mode, 60), governance: governance(d.governance),
      reason: optText(d.reason, 300), order_id: optText(d.order_id, 120), forward_return_60m: optNum(d.forward_return_60m),
      net_pnl: optNum(d.net_pnl), exit_trigger: optText(d.exit_trigger, 60),
      probe: typeof d.probe === "boolean" ? d.probe : null, playbook: optText(d.playbook ?? null, 80),
    };
  }).sort((a, b) => Date.parse(b.decided_at) - Date.parse(a.decided_at));
  return {
    schema: DECISION_QUALITY_SCHEMA,
    tenant_id: text(r.tenant_id, 80),
    generated_at: stamp(r.generated_at),
    window: { since: stamp(window.since), until: stamp(window.until), sessions: num(window.sessions) },
    minimum_sample: num(r.minimum_sample),
    insufficient_sample: bool(r.insufficient_sample),
    counts: {
      decisions: num(counts.decisions), filled: num(counts.filled), rejected: num(counts.rejected),
      abstained: num(counts.abstained), resolved_60m: num(counts.resolved_60m), closed_trades: num(counts.closed_trades),
      probes: counts.probes == null ? null : num(counts.probes),
    },
    rejections: list(r.rejections, 100).map((item) => { const d = record(item); return { reason: text(d.reason, 200), count: num(d.count) }; }),
    directional: {
      horizon_minutes: num(directional.horizon_minutes), evaluated: num(directional.evaluated),
      hit_rate: optNum(directional.hit_rate), mean_forward_return: optNum(directional.mean_forward_return),
    },
    trades: {
      closed: num(trades.closed), win_rate: optNum(trades.win_rate), expectancy: optNum(trades.expectancy),
      profit_factor: optNum(trades.profit_factor), average_win: optNum(trades.average_win), average_loss: optNum(trades.average_loss),
      average_holding_minutes: optNum(trades.average_holding_minutes), net_pnl: num(trades.net_pnl), gross_pnl: num(trades.gross_pnl),
      fees: num(trades.fees),
      exits: list(trades.exits, 100).map((item) => { const d = record(item); return { trigger: text(d.trigger, 120), count: num(d.count) }; }),
    },
    calibration: {
      brier_score: optNum(calibration.brier_score),
      bins: list(calibration.bins, 50).map((item) => {
        const d = record(item);
        return { lower: num(d.lower), upper: num(d.upper), decisions: num(d.decisions), hit_rate: optNum(d.hit_rate), mean_confidence: optNum(d.mean_confidence) };
      }),
    },
    significance: optSignificance(r.significance),
    by_regime: list(r.by_regime, 100).map((item) => {
      const d = record(item);
      return { regime: text(d.regime, 80), decisions: num(d.decisions), filled: num(d.filled), hit_rate: optNum(d.hit_rate), net_pnl: num(d.net_pnl) };
    }),
    by_playbook: list(r.by_playbook ?? [], 100).map((item) => {
      const d = record(item);
      return {
        playbook: text(d.playbook, 80), decisions: num(d.decisions), filled: num(d.filled),
        probes: d.probes == null ? null : num(d.probes), hit_rate: optNum(d.hit_rate), net_pnl: num(d.net_pnl),
      };
    }),
    by_hour_ist: list(r.by_hour_ist, 48).map((item) => {
      const d = record(item);
      return { hour: num(d.hour), decisions: num(d.decisions), hit_rate: optNum(d.hit_rate), net_pnl: num(d.net_pnl) };
    }).sort((a, b) => a.hour - b.hour),
    by_agent: list(r.by_agent, 100).map((item) => {
      const d = record(item);
      return { agent_id: text(d.agent_id, 120), evaluated: num(d.evaluated), directional_accuracy: optNum(d.directional_accuracy) };
    }),
    by_mode: list(r.by_mode, 50).map((item) => { const d = record(item); return { mode: text(d.mode, 80), decisions: num(d.decisions) }; }),
    recent,
    limitations: list(r.limitations, 50).map((item) => text(item, 500)),
    ai_budget: optAiBudget(r.ai_budget),
    inference_health: optInferenceHealth(r.inference_health, num(counts.decisions)),
    forecast_scoring: optForecastScoring(r.forecast_scoring, num(counts.decisions)),
  };
}

/** Parse report text; null on any schema, type or non-finite-number violation. Unknown fields are dropped. */
export function parseDecisionQuality(raw: string): DecisionQualityReport | null {
  try {
    return normalizeReport(JSON.parse(raw));
  } catch {
    return null;
  }
}

function normalizePostMortem(value: unknown, expectedDate?: string): PostMortem {
  const r = record(value);
  if (r.schema !== POST_MORTEM_SCHEMA) fail();
  const session_date = text(r.session_date, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(session_date) || !Number.isFinite(Date.parse(session_date))) fail();
  if (expectedDate && session_date !== expectedDate) fail();
  const status = r.status === "pending" || r.status === "approved" ? r.status : fail();
  const approved_at = r.approved_at === null ? null : stamp(r.approved_at);
  record(r.evidence);
  const s = record(r.summary);
  const counts: PostMortemCounts = {};
  for (const [key, entry] of Object.entries(record(s.counts)).slice(0, 16))
    if (typeof entry === "number" && Number.isFinite(entry)) counts[key.slice(0, 60)] = entry;
  const summary: PostMortemSummary = {
    counts,
    net_pnl: num(s.net_pnl),
    hit_rate_60m: optNum(s.hit_rate_60m),
    exits: list(s.exits, 20).map((item) => { const d = record(item); return {trigger: text(d.trigger, 60), count: num(d.count)}; }),
    rejections: list(s.rejections, 30).map((item) => { const d = record(item); return {reason: text(d.reason, 120), count: num(d.count)}; }),
    by_regime: list(s.by_regime, 20).map((item) => { const d = record(item); return {regime: text(d.regime, 60), decisions: num(d.decisions), filled: num(d.filled), hit_rate: optNum(d.hit_rate), net_pnl: num(d.net_pnl)}; }),
    by_hour_ist: list(s.by_hour_ist, 24).map((item) => { const d = record(item); return {hour: num(d.hour), decisions: num(d.decisions), hit_rate: optNum(d.hit_rate), net_pnl: num(d.net_pnl)}; }),
  };
  return {
    schema: POST_MORTEM_SCHEMA,
    tenant_id: text(r.tenant_id, 80),
    session_date,
    generated_at: stamp(r.generated_at),
    status,
    approved_at,
    summary,
    lessons: list(r.lessons, 50).map((lesson) => truncateLesson(text(lesson, 10_000))),
  };
}

/** Parse one post-mortem; null when malformed. `expectedDate` ties the file name to its session. */
export function parsePostMortem(raw: string, expectedDate?: string): PostMortem | null {
  try {
    return normalizePostMortem(JSON.parse(raw), expectedDate);
  } catch {
    return null;
  }
}

/* ---------- missed opportunities ---------- */

/**
 * The engine's per-session missed-opportunity file: every hold scored against its forward
 * return. On 21 September 2026, the first twelve-name session, every decision was a hold
 * and nothing above could say what the holds had let go by. Same fail-closed parsing as
 * the report: a malformed file yields null, never a guessed number.
 */
export const MISSED_OPPORTUNITIES_SCHEMA = "pramana.missed_opportunities.v1";
export const MISSED_LIMIT_BYTES = 512 * 1024;

export type SpecialistVote = { stance: string; confidence: string };
export type MissedBest = {
  decision_id: string; decided_at: string; reference_price: number | null; forward_return: number;
  regime: string | null; mode: string | null; reason: string | null; agents: Record<string, SpecialistVote>;
};
export type MissedSymbol = { symbol: string; missed: number; avoided: number; evaluated: number; best: MissedBest | null };
export type MissedOpportunities = {
  schema: typeof MISSED_OPPORTUNITIES_SCHEMA;
  tenant_id: string;
  generated_at: string;
  session_date: string;
  threshold: number;
  horizon: string;
  decisions: number;
  holds: number;
  evaluated: number;
  missed: number;
  avoided: number;
  unresolved: number;
  symbols: MissedSymbol[];
  limitations: string[];
};

/** `specialists neutral`, or the dissenters by name, the way the engine's own note words it. */
export function stancesSummary(agents: Record<string, SpecialistVote>): string {
  const entries = Object.entries(agents);
  if (!entries.length) return "no specialist votes";
  const dissent = entries.filter(([, vote]) => vote.stance !== "NEUTRAL").sort(([a], [b]) => a.localeCompare(b));
  if (!dissent.length) return "specialists neutral";
  const clause = dissent.map(([agent, vote]) => `${agent} ${vote.stance}`).join(", ");
  const quiet = entries.length - dissent.length;
  return quiet ? `${clause}, ${quiet} neutral` : clause;
}

function normalizeMissed(value: unknown, expectedDate?: string): MissedOpportunities {
  const r = record(value);
  if (r.schema !== MISSED_OPPORTUNITIES_SCHEMA) fail();
  const session_date = text(r.session_date, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(session_date) || !Number.isFinite(Date.parse(session_date))) fail();
  if (expectedDate && session_date !== expectedDate) fail();
  const symbols = list(r.symbols, 100).map((item) => {
    const d = record(item);
    let best: MissedBest | null = null;
    if (d.best !== null) {
      const b = record(d.best);
      const agents: Record<string, SpecialistVote> = {};
      for (const [agent, vote] of Object.entries(record(b.agents)).slice(0, 50)) {
        const v = record(vote);
        agents[agent.slice(0, 120)] = { stance: text(v.stance, 40), confidence: text(v.confidence, 40) };
      }
      best = {
        decision_id: text(b.decision_id, 120), decided_at: stamp(b.decided_at), reference_price: optNum(b.reference_price),
        forward_return: num(b.forward_return), regime: optText(b.regime, 60), mode: optText(b.mode, 60),
        reason: optText(b.reason, 300), agents,
      };
    }
    return { symbol: text(d.symbol, 40), missed: num(d.missed), avoided: num(d.avoided), evaluated: num(d.evaluated), best };
  });
  return {
    schema: MISSED_OPPORTUNITIES_SCHEMA,
    tenant_id: text(r.tenant_id, 80),
    generated_at: stamp(r.generated_at),
    session_date,
    threshold: num(r.threshold),
    horizon: text(r.horizon, 40),
    decisions: num(r.decisions),
    holds: num(r.holds),
    evaluated: num(r.evaluated),
    missed: num(r.missed),
    avoided: num(r.avoided),
    unresolved: num(r.unresolved),
    symbols,
    limitations: list(r.limitations, 50).map((item) => text(item, 500)),
  };
}

/** Parse one session file; null when malformed. `expectedDate` ties the file name to its session. */
export function parseMissedOpportunities(raw: string, expectedDate?: string): MissedOpportunities | null {
  try {
    return normalizeMissed(JSON.parse(raw), expectedDate);
  } catch {
    return null;
  }
}
