import {createHash} from "node:crypto";
import type {Portfolio} from "./types";

type Observation = {date: string; close: number};
type Instrument = {symbol: string; market: string; assetClass: string; currency: string; exchange: string; providerInstrumentId: string; observations: Observation[]};
type Input = {schema: string; asOf: string; source: string; priceBasis: string;
  calendar: {name: string; timezone: string; coverageStart: string; coverageEnd: string; version: string; sessions: string[]; specialSessions?: string[]}; instruments: Instrument[]};
export type HistoricalRiskRow = {key: string; symbol: string; assetClass: string; providerInstrumentId: string; marketValue: number; equityWeight: number;
  observations: number; missingDates: string[]; dailyVolatility: number | null; volatilityContribution: number | null; varianceShare: number | null};
export type HistoricalRiskReport = {
  schema: "pramana.historical_risk.v1"; qualification: "exploratory"; sourceSha256: string;
  source: string; sourceAsOf: string; valuationAsOf: string; calendarVersion: string; priceBasis: string;
  from: string; through: string; intervals: number; totalEquity: number; cashWeight: number;
  dailyVolatility: number; diversificationRatio: number | null; rows: HistoricalRiskRow[];
  correlation: (number | null)[][]; covariance: number[][];
  historicalLoss: {confidence: 0.95; sampleCount: number; tailMass: number; var: number | null; expectedShortfall: number | null; reason: string | null; worst: number};
  scenarios: {from: string; through: string; pnl: number; equityReturn: number}[];
  limitations: string[];
};
export type HistoricalRiskState = {status: "available" | "incomplete" | "unavailable" | "invalid"; detail: string;
  rows: HistoricalRiskRow[]; report: HistoricalRiskReport | null};
class RiskInputError extends Error {}
const requireValue = (condition: unknown, message: string): void => {if (!condition) throw new RiskInputError(message);};
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const text = (v: unknown) => typeof v === "string" && v.length > 0 && v.length <= 160;
const date = (v: unknown): v is string => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) && Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0,10) === v;
const timestamp = (v: unknown) => typeof v === "string" && /^\d{4}-\d\d-\d\dT/.test(v) && /(Z|[+-]\d\d:\d\d)$/.test(v) && Number.isFinite(Date.parse(v));
const key = (i: {market: string; assetClass: string; symbol: string}) => JSON.stringify([i.market,i.assetClass,i.symbol]);
const near = (a: number,b: number) => Math.abs(a-b) <= 1e-8 + 32 * Number.EPSILON * Math.max(1,Math.abs(a),Math.abs(b));
const mean = (xs: number[]) => xs.reduce((a,b)=>a+b,0)/xs.length;

/** Read-only diagnostic: no order sizing, readiness promotion or provider calls. */
export function historicalRisk(p: Portfolio, raw: unknown, now = Date.now()): HistoricalRiskState {
  const absent = (detail: string, rows: HistoricalRiskRow[] = [], status: HistoricalRiskState["status"] = "unavailable"): HistoricalRiskState => ({status,detail,rows,report:null});
  if (!raw) return absent("No risk-history dataset. The market collector must publish completed, source-labelled daily closes and its session calendar.");
  try {
    requireValue(p.currency === "INR" && p.markMode === "engine_live" && p.status === "ok" && finite(p.totalEquity) && p.totalEquity > 0 && finite(p.cash) && p.cash >= 0, "A current reconciled INR cash portfolio is required.");
    const stamp = Date.parse(p.updatedAt ?? "");
    requireValue(timestamp(p.updatedAt) && now-stamp >= -5000 && now-stamp <= 30000, "Portfolio valuation is stale or has an invalid timestamp.");
    requireValue(Array.isArray(p.holdings) && p.holdings.length <= 40, "The diagnostic supports at most 40 holdings; no exposures are omitted.");
    requireValue(p.holdings.every(h => h.market === "INDIA" && ["EQUITY","ETF"].includes(h.assetClass) && text(h.symbol) && Number.isSafeInteger(h.quantity) && h.quantity > 0 && finite(h.markPrice) && h.markPrice > 0 && finite(h.marketValue) && near(h.marketValue,h.quantity*h.markPrice) && h.fresh === true && h.markSource === "live_tick" && stamp-Date.parse(h.markTimestamp ?? "") >= 0 && stamp-Date.parse(h.markTimestamp ?? "") <= 120000), "Every holding requires a fresh, positive, consistent NSE cash-equity/ETF mark.");
    requireValue(new Set(p.holdings.map(key)).size === p.holdings.length && near(p.cash+p.holdings.reduce((s,h)=>s+h.marketValue,0),p.totalEquity), "Holding identities or cash/equity do not reconcile.");
    if (!p.holdings.length) return absent("Cash-only account: no invested instruments to correlate. This does not assess inflation, custody or future trading risk.");
    const input = raw as Input;
    if ((raw as {status?: string}).status === "unavailable") return absent("Risk-history producer is unavailable; inspect collector/calendar qualification.");
    requireValue(input.schema === "pramana.risk_history.v1" && text(input.source) && input.priceBasis === "provider_close_adjustments_unverified", "Unsupported or missing risk-history provenance.");
    const captured = Date.parse(input.asOf);
    requireValue(timestamp(input.asOf) && now-captured >= -5000 && now-captured <= 36*3600000, "Risk-history capture is older than 36 hours or invalid.");
    const localDay = new Date(captured+19800000).toISOString().slice(0,10);
    const cal = input.calendar;
    requireValue(cal && text(cal.name) && text(cal.version) && cal.timezone === "Asia/Kolkata" && date(cal.coverageStart) && date(cal.coverageEnd) && cal.coverageStart <= localDay && cal.coverageEnd >= localDay, "Session-calendar coverage is absent or expired.");
    requireValue(Array.isArray(cal.sessions) && cal.sessions.length > 0 && cal.sessions.length <= 1000, "A bounded completed-session calendar is required.");
    const special = cal.specialSessions ?? [];
    // Explicitly verified NSE/CMTR/72349 exception; arbitrary weekends are not accepted.
    requireValue(Array.isArray(special) && special.length <= 1 && special.every(d=>d==="2026-02-01"), "Unsupported special-session calendar; exchange evidence needs review.");
    requireValue(cal.sessions.every((d,i) => date(d) && d >= cal.coverageStart && d < localDay && (!i || d > cal.sessions[i-1]) && (![0,6].includes(new Date(d).getUTCDay()) || special.includes(d))), "Calendar sessions must be unique, ordered, completed weekdays or the documented Budget Sunday.");
    requireValue(Date.parse(localDay)-Date.parse(cal.sessions.at(-1)!) <= 7*86400000, "The last declared session is older than seven days; calendar/source coverage needs review.");
    requireValue(Array.isArray(input.instruments) && input.instruments.length <= 500 && input.instruments.every(i => i && text(i.symbol) && text(i.market) && text(i.assetClass)), "Invalid instrument history collection.");
    requireValue(new Set(input.instruments.map(key)).size === input.instruments.length, "Duplicate instrument histories are ambiguous.");
    const sessions = cal.sessions.slice(-253), sessionSet = new Set(cal.sessions);
    const rows: HistoricalRiskRow[] = [], series: Map<string,number>[] = [];
    for (const h of [...p.holdings].sort((a,b)=>key(a).localeCompare(key(b)))) {
      const item = input.instruments.find(i => key(i) === key(h));
      const values = new Map<string,number>();
      if (item) {
        requireValue(item.currency === "INR" && item.exchange === "NSE" && text(item.providerInstrumentId), "History currency, venue or provider identity does not match the holding.");
        requireValue(Array.isArray(item.observations) && item.observations.length <= 1000, "Instrument history exceeds the bounded observation limit.");
        requireValue(item.observations.every((o,i) => o && date(o.date) && sessionSet.has(o.date) && finite(o.close) && o.close > 0 && (!i || o.date > item.observations[i-1].date)), "Daily closes must be positive, unique, ordered and on completed declared sessions.");
        item.observations.forEach(o=>values.set(o.date,o.close));
      }
      const missingDates = sessions.filter(d=>!values.has(d));
      rows.push({key:key(h),symbol:h.symbol,assetClass:h.assetClass,providerInstrumentId:item?.providerInstrumentId ?? "unavailable",marketValue:h.marketValue,equityWeight:h.marketValue/p.totalEquity,observations:sessions.length-missingDates.length,missingDates,dailyVolatility:null,volatilityContribution:null,varianceShare:null});
      series.push(values);
    }
    const providerIds = rows.filter(r=>r.providerInstrumentId!=="unavailable").map(r=>r.providerInstrumentId);
    requireValue(new Set(providerIds).size===providerIds.length, "Selected holdings share a provider instrument identity; mapping needs review.");
    if (rows.some(r=>r.missingDates.length)) return absent("Missing holding/session closes: aggregate risk is withheld. No pairwise deletion, forward fill or renormalization of covered holdings is used.",rows,"incomplete");
    const n = sessions.length-1;
    if (n < 60) return absent(`Only ${Math.max(0,n)} complete daily intervals; at least 60 are required for this exploratory covariance diagnostic.`,rows,"incomplete");
    const returns = series.map(s=>sessions.slice(1).map((d,t)=>s.get(d)!/s.get(sessions[t])!-1));
    requireValue(returns.every(xs=>xs.every(finite)), "Daily return arithmetic is nonfinite.");
    const means = returns.map(mean);
    const cov = returns.map((a,i)=>returns.map((b,j)=>a.reduce((v,x,t)=>v+(x-means[i])*(b[t]-means[j]),0)/(n-1)));
    const weights = rows.map(r=>r.equityWeight);
    const covWeights = cov.map(row=>row.reduce((s,c,j)=>s+c*weights[j],0));
    const variance = weights.reduce((s,w,i)=>s+w*covWeights[i],0);
    requireValue(finite(variance) && variance >= -1e-12, "Covariance aggregation is invalid.");
    const sigma = Math.sqrt(Math.max(0,variance));
    const correlation = cov.map((r,i)=>r.map((c,j)=>cov[i][i] > 0 && cov[j][j] > 0 ? Math.max(-1,Math.min(1,c/Math.sqrt(cov[i][i]*cov[j][j]))) : null));
    rows.forEach((r,i)=>{r.dailyVolatility=Math.sqrt(cov[i][i]);r.volatilityContribution=sigma > 1e-12 ? weights[i]*covWeights[i]/sigma : null;r.varianceShare=variance > 1e-24 ? weights[i]*covWeights[i]/variance : null;});
    const scenarios = sessions.slice(1).map((through,t)=>{const pnl=rows.reduce((s,r,i)=>s+r.marketValue*returns[i][t],0);return {from:sessions[t],through,pnl,equityReturn:pnl/p.totalEquity};});
    const losses = scenarios.map(s=>-s.pnl).sort((a,b)=>a-b);
    requireValue(losses.every(finite) && cov.every(r=>r.every(finite)), "Historical scenario arithmetic is nonfinite.");
    const tailMass = n * .05, descending = [...losses].reverse(), whole = Math.floor(tailMass);
    const es = (descending.slice(0,whole).reduce((s,v)=>s+v,0)+(tailMass-whole)*(descending[whole] ?? 0))/tailMass;
    const report: HistoricalRiskReport = {
      schema:"pramana.historical_risk.v1",qualification:"exploratory",
      sourceSha256:createHash("sha256").update(JSON.stringify({portfolio:p,input})).digest("hex"),
      source:input.source,sourceAsOf:input.asOf,valuationAsOf:p.updatedAt!,calendarVersion:cal.version,priceBasis:input.priceBasis,
      from:sessions[0],through:sessions.at(-1)!,intervals:n,totalEquity:p.totalEquity,cashWeight:p.cash/p.totalEquity,
      dailyVolatility:sigma,diversificationRatio:sigma>1e-12 ? rows.reduce((s,r)=>s+r.equityWeight*r.dailyVolatility!,0)/sigma : null,
      rows,correlation,covariance:cov,
      historicalLoss:{confidence:.95,sampleCount:n,tailMass,var:n>=100 ? losses[Math.ceil(.95*n)-1] : null,expectedShortfall:n>=100 ? es : null,reason:n>=100 ? null : "At least 100 complete intervals (five tail observations) are required for the 95% historical loss display.",worst:losses.at(-1)!},
      scenarios:scenarios.sort((a,b)=>a.pnl-b.pnl),
      limitations:[
        "Exploratory current-holdings repricing, not actual account history, a forecast, a loss limit or strategy qualification.",
        "Provider close adjustments, instrument mapping and the declared session calendar are not independently qualified. Splits/dividends or omissions can distort results.",
        "Fixed current INR holdings and zero-return cash; no rebalancing, exits, liquidity, fees, intraday gaps, FX, leverage or derivatives.",
        "Sample covariance uses the same complete adjacent-session returns for every holding, with n−1 denominator; no annualization or normal-distribution assumption.",
        "95% VaR uses nearest-rank observed loss; expected shortfall integrates the worst 5% with fractional boundary weight. Negative loss values mean gains in that sample.",
        "60/100-interval floors are display safeguards, not statistical sufficiency. Historical tails and correlations can change; losses may exceed every observed scenario.",
        "Local source hashes identify inputs; they do not authenticate provider data or prevent coordinated rewriting.",
      ],
    };
    return {status:"available",detail:"Exploratory historical risk using complete declared sessions. Source adjustments and forward risk are unqualified.",rows,report};
  } catch (error) {return absent(error instanceof RiskInputError ? error.message : "Risk history could not be validated.",[],"invalid");}
}

export function historicalRiskContext(state: HistoricalRiskState) {
  if (!state.report) return {...state,report:null};
  const {scenarios,covariance,correlation,...report}=state.report;
  return {...state,report:{...report,correlation,worstScenarios:scenarios.slice(0,10),omittedScenarios:Math.max(0,scenarios.length-10)}};
}
