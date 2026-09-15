export type Observation = {date: string; close: number};
export type Instrument = {symbol: string; market: string; assetClass: string; currency: string; exchange: string; providerInstrumentId: string; observations: Observation[]};
export type Input = {schema: string; asOf: string; source: string; priceBasis: string;
  calendar: {name: string; timezone: string; coverageStart: string; coverageEnd: string; version: string; sessions: string[]; specialSessions?: string[]}; instruments: Instrument[]};

export class DailyHistoryError extends Error {}
export const requireValue = (condition: unknown, message: string): void => {if (!condition) throw new DailyHistoryError(message);};
export const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
export const text = (v: unknown) => typeof v === "string" && v.length > 0 && v.length <= 160;
const date = (v: unknown): v is string => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) && Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0,10) === v;
export const timestamp = (v: unknown) => typeof v === "string" && /^\d{4}-\d\d-\d\dT/.test(v) && /(Z|[+-]\d\d:\d\d)$/.test(v) && Number.isFinite(Date.parse(v));
export const historyKey = (i: {market: string; assetClass: string; symbol: string}) => JSON.stringify([i.market,i.assetClass,i.symbol]);

/** Shared validation for completed-session source evidence; never fills data gaps. */
export function validateDailyHistory(raw: unknown, now = Date.now()): Input {
    const input = raw as Input;
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
    requireValue(new Set(input.instruments.map(historyKey)).size === input.instruments.length, "Duplicate instrument histories are ambiguous.");
    return input;
}
