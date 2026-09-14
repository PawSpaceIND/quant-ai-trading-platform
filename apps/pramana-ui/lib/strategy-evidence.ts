import { hasTable, openLedger, tenantId } from "./db";
import type { Runtime } from "./pilot";

/** Read distinct, configuration-matched minute samples; this is not a return attribution model. */
export function strategyObservationDays(sha: string | undefined, incompatibleDates: string[] = [], coverageStartedAt?: string) {
  const empty = {days:0, daily:[] as {date:string;minutes:number}[], source:"configuration_matched_paper_observations"};
  const coverageStart = Date.parse(coverageStartedAt ?? "");
  if (!sha || !/^[0-9a-f]{64}$/.test(sha) || !Number.isFinite(coverageStart)) return empty;
  let db: ReturnType<typeof openLedger> = null;
  try {
    db = openLedger();
    if (!db || !hasTable(db,"paper_live_valuations")) return empty;
    const rows = db.prepare("SELECT payload FROM paper_live_valuations WHERE tenant_id=? ORDER BY timestamp").all(tenantId) as {payload:string}[];
    const days = new Map<string,{minutes:Set<number>;lastMinute:number;incompatible:boolean}>();
    const today = new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Kolkata",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
    const invalid = new Set(incompatibleDates);
    for (const row of rows) {
      const p = JSON.parse(row.payload);
      if (p.qualifyingSession !== true || !p.sessionDate || p.sessionDate >= today) continue;
      const timestamp = Date.parse(p.updatedAt);
      if (!Number.isFinite(timestamp) || timestamp < coverageStart) continue;
      const local = new Date(timestamp + 330*60000);
      const date = local.toISOString().slice(0,10), minute=local.getUTCHours()*60+local.getUTCMinutes();
      if (date !== p.sessionDate || minute < 9*60+15 || minute >= 15*60+30) continue;
      const d = days.get(date) ?? {minutes:new Set<number>(),lastMinute:0,incompatible:invalid.has(date)};
      if (p.strategyObservation?.manifestSha256 !== sha) d.incompatible=true;
      else if (p.strategyObservation.eligible === true && p.allMarksFresh === true && Number.isFinite(p.totalEquity) && p.totalEquity>0) {
        d.minutes.add(Math.floor(timestamp/60000)); d.lastMinute=Math.max(d.lastMinute,minute);
      }
      days.set(date,d);
    }
    const daily=[...days.entries()].filter(([,d])=>!d.incompatible && d.minutes.size>=300 && d.lastMinute>=15*60+25)
      .map(([date,d])=>({date,minutes:d.minutes.size}));
    return {...empty,days:daily.length,daily};
  } catch { return empty; }
  finally { db?.close(); }
}

export function currentStrategyEvidence(runtime: Runtime, sha: string) {
  const evidence=runtime.strategyEvidence;
  if (!evidence || evidence.schema!=="pramana.strategy_episode_evidence.v1" || !Number.isFinite(Date.parse(evidence.coverageStartedAt)) || evidence.status!=="ok" || evidence.strategySha256!==sha
      || !/^[0-9a-f]{64}$/.test(evidence.evidenceSha256) || !/^[0-9a-f]{64}$/.test(evidence.sourceSha256)
      || !Number.isInteger(evidence.ledgerId) || !Number.isInteger(evidence.summary?.completedTrades)
      || evidence.unresolvedEpisodes!==0 || evidence.foreignOpenEpisodes!==0) return false;
  let db: ReturnType<typeof openLedger> = null;
  try {
    db=openLedger();
    if (!db || !hasTable(db,"paper_ledger")) return false;
    const row=db.prepare("SELECT COALESCE(MAX(id),0) AS id FROM paper_ledger WHERE tenant_id=?").get(tenantId) as {id:number};
    return row.id===evidence.ledgerId;
  } catch { return false; } finally { db?.close(); }
}

export function sameEvidenceMetric(a: unknown,b: unknown) {
  const valid=(v:unknown)=> (typeof v==="string" && v.trim()!=="" || typeof v==="number") && Number.isFinite(Number(v));
  return valid(a) && valid(b) && Math.abs(Number(a)-Number(b)) <= 1e-9*Math.max(1,Math.abs(Number(b)));
}
