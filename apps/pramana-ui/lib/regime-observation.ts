import { sourceAge } from "./freshness";

export type RegimeFrame = {
  timeframe: "1d" | "15m"; label: string; barsUsed: number; barsAvailable: number;
  classified: boolean;
};
export type RegimeObservation = {
  observedAt: string; ageSeconds: number; technicalBars: number; minimumBars: number;
  lookbackBars: number; daily: RegimeFrame; intraday: RegimeFrame;
  selectedTimeframe: "1d" | "15m"; selectedLabel: string; selectionReason: string;
};
const labels = new Set(["insufficient_history", "ranging", "trending_up", "trending_down", "high_volatility"]);
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const count = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 1000000;

/** A stored observation is historical context, never proof of trading readiness. */
export function readRegimeObservation(value: unknown, now = Date.now()): RegimeObservation | null {
  if (!object(value) || value.schema !== "pramana.regime_observation.v1" || value.state !== "observed" ||
      value.technicalTimeframe !== "1m" || !count(value.technicalBars) ||
      !count(value.minimumBars) || !count(value.lookbackBars) || value.minimumBars < 1 ||
      value.lookbackBars < value.minimumBars || typeof value.observedAt !== "string") return null;
  const age = sourceAge(value.observedAt, now);
  if (age === null || age < 0) return null;
  const frame = (raw: unknown, timeframe: "1d" | "15m"): RegimeFrame | null => {
    if (!object(raw) || raw.timeframe !== timeframe || typeof raw.label !== "string" || !labels.has(raw.label) ||
        !count(raw.barsUsed) || !count(raw.barsAvailable) ||
        raw.barsUsed !== Math.min(raw.barsAvailable, value.lookbackBars as number) ||
        raw.classified !== (raw.label !== "insufficient_history") ||
        raw.classified !== (raw.barsUsed >= (value.minimumBars as number))) return null;
    return {timeframe, label:raw.label, barsUsed:raw.barsUsed, barsAvailable:raw.barsAvailable, classified:raw.classified as boolean};
  };
  const daily = frame(value.daily, "1d"), intraday = frame(value.intraday, "15m");
  if (!daily || !intraday) return null;
  const selected = daily.classified ? daily : intraday.classified ? intraday :
    daily.barsUsed >= intraday.barsUsed ? daily : intraday;
  const reason = daily.classified ? "daily_classified" : intraday.classified ? "intraday_fallback" :
    "neither_classified_most_bars_daily_tiebreak";
  if (value.selectedTimeframe !== selected.timeframe || value.selectedLabel !== selected.label || value.selectionReason !== reason) return null;
  return {observedAt:value.observedAt, ageSeconds:age, technicalBars:value.technicalBars,
    minimumBars:value.minimumBars, lookbackBars:value.lookbackBars, daily, intraday,
    selectedTimeframe:selected.timeframe, selectedLabel:selected.label, selectionReason:reason};
}
