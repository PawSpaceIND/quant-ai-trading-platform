export type Participation = { status: string; label: string; reason: string; role: string };

const reasons: Record<string, [string, string]> = {
  non_us_market: ["not_applicable", "This instrument is outside the US stock specialist's scope."],
  non_india_market: ["not_applicable", "This instrument is outside the Indian stock specialist's scope."],
  non_equity_instrument: ["not_applicable", "Company valuation does not apply to this asset type."],
  non_equity_macro_model: ["not_applicable", "This macro model describes effects on individual stocks."],
  non_etf_instrument: ["not_applicable", "Fund-value analysis applies to ETFs only."],
  india_fundamentals_insufficient: ["missing_data", "Too few Indian company valuation inputs."],
  us_fundamentals_insufficient: ["missing_data", "Too few US company valuation inputs."],
  insufficient_price_history: ["missing_data", "Not enough price history for this specialist."],
  required_source_unavailable: ["stale_or_missing", "A required source is stale or missing."],
  desk_inputs_missing: ["missing_data", "The control lacks required market or portfolio inputs."],
  zero_conviction: ["abstained", "No usable conviction was recorded."],
  specialist_veto: ["veto", "This specialist refused the proposed risk."],
  gate_clear: ["ready", "Control checked; this is not a directional vote."],
  informed_neutral: ["ready", "Evidence was available, but no direction was preferred."],
  directional_view: ["ready", "A directional view was recorded; it remains subject to trading controls."],
  etf_reference_observed: ["observed", "Fresh indicative fund value was compared with the quote. Reference only, not a buy signal."],
  etf_reference_unconfigured: ["missing_data", "No indicative fund-value source is configured."],
  etf_reference_missing: ["missing_data", "No fund-value observation matches this instrument and currency."],
  etf_reference_invalid: ["missing_data", "The fund-value observation failed validation."],
  etf_reference_stale: ["missing_data", "The fund-value observation is too old."],
  etf_reference_quote_unavailable: ["missing_data", "No matching fresh market quote for the value comparison."],
};
const labels: Record<string, string> = {not_applicable: "Not applicable", missing_data: "Missing data",
  stale_or_missing: "Source unavailable", abstained: "Abstained", veto: "Veto", ready: "Ready", observed: "Reference only"};

export function specialistParticipation(row: Record<string, unknown>): Participation {
  const role = ["gate", "directional", "reference"].includes(String(row.role)) ? String(row.role) : "unrecorded";
  const key = typeof row.reason_code === "string" ? row.reason_code : "";
  const entry = Object.hasOwn(reasons, key) ? reasons[key] : undefined;
  if (!entry || row.participation !== entry[0]) return {status: "unrecorded", label: "Reason not recorded",
    reason: "This older or incomplete proof does not explain participation; 0% alone cannot establish why.", role};
  return {status: entry[0], label: labels[entry[0]], reason: entry[1], role};
}

export const providerFailures: Record<string, string> = {
  provider_timeout: "The provider timed out.", provider_auth: "The provider rejected authentication or access.",
  provider_rate_limited: "The provider rate limit was reached.", provider_overloaded: "The provider was overloaded.",
  provider_unavailable: "The provider was unavailable; a more specific cause was not recorded.",
  budget_exhausted: "The configured AI budget was exhausted.", output_truncated: "The model response exceeded its output allowance.",
  model_refusal: "The model refused the request.", incomplete_turn: "The model response did not complete.",
  completion_unverified: "A complete model response could not be verified.",
};
