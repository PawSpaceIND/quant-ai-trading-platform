import type { Runtime } from "./pilot";
import type { Portfolio, Workspace } from "./types";

/** Source age, preserving microseconds and rejecting timezone-less or normalized dates. */
export function sourceAge(value: unknown, now = Date.now()): number | null {
  if (typeof value !== "string" || !Number.isSafeInteger(now) ||
      !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value)) return null;
  const stamp=Date.parse(value),zone=value.endsWith("Z")?null:value.slice(-6);
  const offset=zone?(zone[0]==="-"?-1:1)*(Number(zone.slice(1,3))*60+Number(zone.slice(4))):0;
  if (!Number.isFinite(stamp) || value.startsWith("0000-") ||
      new Date(stamp+offset*60000).toISOString().slice(0,19)!==value.slice(0,19)) return null;
  const fraction=(value.match(/\.(\d+)(?:Z|[+-])/)?.[1]??"").padEnd(6,"0");
  const micros=BigInt(stamp)*BigInt(1000)+BigInt(fraction.slice(3));
  return Number(BigInt(now)*BigInt(1000)-micros)/1e6;
}
export const within = (age: number | null, seconds: number, futureTolerance = 0) =>
  age !== null && age >= -futureTolerance && age <= seconds;

export function ageRuntime(data: Runtime, now = Date.now()): Runtime {
  const current=within(sourceAge(data.updatedAt,now),10,5);
  const status=current?data.status:"stale";
  const running=status==="running"&&data.mode==="paper";
  const rows=Array.isArray(data.watchlist)?data.watchlist:[];
  const valid=(data.watchlist===undefined||Array.isArray(data.watchlist))&&rows.length<=500&&rows.every(r=>r&&typeof r==="object"&&
    [r.symbol,r.market,r.assetClass,r.currency,r.exchange].every(v=>typeof v==="string"&&v.length>0&&v.length<=100));
  const keys=rows.map(r=>r?`${r.market}:${r.exchange}:${r.assetClass}:${r.symbol}`:"");
  const seen=new Set<string>(),duplicates=new Set<string>();for(const key of keys){if(seen.has(key))duplicates.add(key);seen.add(key);}
  return {...data,status:valid?status:"invalid",...(!valid?{runtimeEvidenceIssue:"Engine watchlist evidence is malformed or exceeds 500 instruments; coverage is unverified."}:{}),watchlist:valid?rows.map((row,index)=>{
    const age=sourceAge(row.tickTimestamp,now);
    const duplicate=duplicates.has(keys[index]);
    const fresh=running&&!duplicate&&row.fresh===true&&within(age,120);
    const freshnessReason=duplicate?"duplicate_instrument":!running?"engine_unverified":age===null?"timestamp_unrecorded_or_invalid":age<0?"future_tick":age>120?"tick_expired":row.fresh!==true?"engine_rejected_tick":"fresh";
    return {...row,fresh,tickAgeSeconds:age,freshnessReason};
  }):[]};
}

export function tradingFeedCheck(data: Runtime, now=Date.now()) {
  const runtime=ageRuntime(data,now);
  const rows=runtime.watchlist??[];
  const identities=rows.map(r=>`${r.market}:${r.exchange}:${r.assetClass}:${r.symbol}`);
  const fresh=rows.filter(r=>r.fresh===true).length;
  return {id:"ticks",title:"Trading feed coverage",
    pass:runtime.status==="running"&&runtime.mode==="paper"&&rows.length>0&&fresh===rows.length&&new Set(identities).size===rows.length,
    detail:runtime.runtimeEvidenceIssue??`${fresh}/${rows.length} engine instruments have verified ticks. Requires a current engine heartbeat and source timestamps no older than 120 seconds; missing or future timestamps cannot qualify.`};
}

export function agePortfolio<T extends Pick<Portfolio,"status"|"markMode"|"markDisclaimer"|"updatedAt"|"holdings"> & {allMarksFresh?:boolean}>(p:T,now=Date.now()):T {
  const current=p.markMode==="engine_live"&&["ok","degraded"].includes(p.status)&&within(sourceAge(p.updatedAt,now),30,5);
  // Older cash-only snapshots did not serialize an empty holdings array. Keep
  // that shape readable, while an explicitly malformed holdings value remains
  // invalid and cannot claim a current mark.
  const legacyEmpty=p.holdings===undefined;
  const valid=legacyEmpty||(Array.isArray(p.holdings)&&p.holdings.every(h=>h&&typeof h==="object"));
  const holdings=valid?(legacyEmpty?[]:p.holdings.map(h=>({...h,fresh:current&&h.fresh===true&&Number.isFinite(h.markPrice)&&h.markPrice>0&&h.markSource==="live_tick"&&within(sourceAge(h.markTimestamp,now),120)}))):[];
  const allFresh=valid&&p.allMarksFresh!==false&&holdings.every(h=>h.fresh);
  const status=!valid?"invalid":p.status==="invalid"?"invalid":p.markMode!=="engine_live"?p.status:!current?"stale":!allFresh?"degraded":p.status;
  return {...p,status,holdings,allMarksFresh:current&&p.allMarksFresh!==false&&allFresh,
    markDisclaimer:p.markMode!=="engine_live"||p.status==="invalid"?p.markDisclaimer:!current?"Engine valuation is no longer current. Amounts are last observed values; refresh source evidence before using them.":!allFresh?"One or more held marks lack a current source timestamp. Amounts remain last observed values.":p.markDisclaimer};
}

/** Downgrade cached current-state claims without changing immutable historical reports. */
export function ageWorkspace(snapshot:Workspace,now=Date.now()):Workspace {
  const responseCurrent=within(sourceAge(snapshot.generatedAt,now),30,5);
  let runtime=ageRuntime(snapshot.runtime,now),portfolio=agePortfolio(snapshot.portfolio,now);
  if(!responseCurrent){
    runtime={...runtime,status:"stale",watchlist:runtime.watchlist?.map(r=>({...r,fresh:false,freshnessReason:"workspace_expired"}))};
    portfolio={...portfolio,status:portfolio.status==="invalid"?"invalid":"stale",holdings:portfolio.holdings.map(h=>({...h,fresh:false})),markDisclaimer:"Workspace updates are overdue. These are last observed amounts, not current valuations."};
  }
  const market={...snapshot.market,collectorStale:snapshot.market.collectorStale===true||!within(sourceAge(snapshot.market.fetchedAt,now),120,5)};
  const engine=runtime.status==="running"&&runtime.mode==="paper";
  const currentBook=responseCurrent&&portfolio.markMode==="engine_live"&&portfolio.status==="ok";
  const feed=tradingFeedCheck(runtime,now);
  const engineChecks=new Set(["engine","entry_controls","strategy_manifest","strategy_evidence","reconciliation","protection_coverage","protection_sweep","evidence"]);
  const manifest=runtime.strategyManifest,manifestAge=sourceAge(manifest?.checkedAt,now);
  const manifestCurrent=engine&&within(manifestAge,10,5)&&typeof manifest?.sourceCheckAgeSeconds==="number"&&Number.isFinite(manifest.sourceCheckAgeSeconds)&&manifest.sourceCheckAgeSeconds>=0&&manifest.sourceCheckAgeSeconds+Math.max(manifestAge??Infinity,0)<=65;
  const checks=snapshot.checks.map(c=>{
    const allowed=responseCurrent&&(c.id==="ticks"?feed.pass:c.id==="quotes"?!market.collectorStale:c.id==="marks"?currentBook:["strategy_manifest","strategy_evidence","evidence"].includes(c.id)?manifestCurrent:c.id==="reconciliation"?engine&&within(sourceAge(runtime.reconciliation?.checkedAt,now),120,5):c.id==="protection_coverage"?engine&&currentBook&&within(sourceAge(runtime.protectionCoverage?.checkedAt,now),10,5):c.id==="protection_sweep"?engine&&within(sourceAge(runtime.protectionSweep?.checkedAt,now),10,5):engineChecks.has(c.id)?engine:true);
    return {...c,pass:c.pass===true&&allowed,detail:!responseCurrent?"Workspace evidence expired. Refresh is required to verify this check.":c.id==="ticks"?feed.detail:c.id==="marks"?portfolio.markDisclaimer:!allowed&&engineChecks.has(c.id)?"Engine or source evidence is no longer current. Refresh is required to verify this check.":c.detail};
  });
  const contribution=snapshot.paperContribution;
  const contributionCurrent=responseCurrent&&within(sourceAge(portfolio.updatedAt,now),30,5)&&contribution?.report?.rows.every(r=>r.markState!=="fresh"||portfolio.holdings.some(h=>h.market===r.market&&h.assetClass===r.assetClass&&h.symbol===r.symbol&&h.fresh));
  return {...snapshot,runtime,portfolio,market,checks,
    attribution:currentBook?snapshot.attribution:snapshot.attribution?{status:"unavailable",detail:"Portfolio marks expired or are incomplete. Refresh before using sector/factor exposure."}:undefined,
    historicalRisk:currentBook?snapshot.historicalRisk:snapshot.historicalRisk?{status:"unavailable",detail:"Current portfolio evidence expired or is incomplete. Refresh before using current-holding historical risk estimates.",rows:[],report:null}:undefined,
    paperContribution:contribution?.report&&!contributionCurrent?{status:"incomplete",detail:"Current contribution evidence expired. Refresh source valuations before using current P&L totals.",report:null}:contribution};
}
