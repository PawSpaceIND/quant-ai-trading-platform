import {createHash} from "node:crypto";
import type {DatabaseSync} from "node:sqlite";
import {tenantId, type CostRow, type LedgerRow} from "./db";
import type {PaperContributionState} from "./paper-contribution";
import {validateDailyHistory, DailyHistoryError, requireValue, finite, text, timestamp, historyKey} from "./daily-history";
import {BENCHMARKS, type AccountBenchmarkState, type AccountBenchmarkReport, type BenchmarkDay} from "./benchmark-comparison";

export const benchmarkUnavailable = (detail: string): AccountBenchmarkState => ({status:"unavailable",detail,report:null});
const local = (ms:number) => new Date(ms+19800000).toISOString();
const digest = (value:unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

/** Caller holds the same read transaction used to validate the whole recorded account. */
export function readAccountBenchmark(db:DatabaseSync, account:PaperContributionState, raw:unknown, now=Date.now()):AccountBenchmarkState {
  if (!account.report) return {status:account.status==="invalid" || account.status==="outdated" ? "invalid" : "unavailable", detail:`A reconciled recorded account is required. ${account.detail}`,report:null};
  if (!raw || (raw as {status?:string}).status==="unavailable") return benchmarkUnavailable("Completed, source-labelled daily closes and a session calendar are required for account comparison.");
  try {
    const input=validateDailyHistory(raw,now), validated=account.report;
    requireValue(timestamp(validated.asOf),"Account valuation requires an explicit timestamp offset.");
    const accountLocal=local(Date.parse(validated.asOf)), accountDate=accountLocal.slice(0,10), accountTime=accountLocal.slice(11,19);
    const eligible=input.calendar.sessions.filter(d=>d<accountDate || d===accountDate && accountTime>="15:30:00");
    if (!eligible.length) return benchmarkUnavailable("No completed source session precedes the account valuation.");
    const through=eligible.at(-1)!;
    const entries=db.prepare("SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id LIMIT 20001").all(tenantId) as LedgerRow[];
    const costs=db.prepare("SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id LIMIT 200001").all(tenantId) as CostRow[];
    requireValue(entries.length===validated.fillCount && entries.length<=20000 && costs.length===validated.costRowCount && costs.length<=200000 && (entries.at(-1)?.id ?? 0)===validated.ledgerId,"Recorded account changed outside the shared read transaction.");
    const fees=new Map<string,number>();
    for (const c of costs) if (c.cash_debit===1) fees.set(c.order_id,(fees.get(c.order_id) ?? 0)+Number(c.amount));
    const declared=new Set(input.calendar.sessions);
    const fills=entries.map(e=>{
      requireValue(timestamp(e.created_at),"Fill timestamp needs an explicit timezone; historical ordering cannot be inferred.");
      const at=local(Date.parse(e.created_at)); return {...e,day:at.slice(0,10),time:at.slice(11,19)};
    }).filter(e=>e.day<=through);
    if (!fills.length) return benchmarkUnavailable("No recorded fills in completed sessions. Historical account inception is not inferred from an empty ledger.");
    if (fills[0].day<input.calendar.coverageStart) return benchmarkUnavailable("Recorded account history starts before the declared calendar coverage; qualify earlier sessions first.");
    requireValue(fills.every(e=>declared.has(e.day) && e.time>="09:15:00" && e.time<"15:30:00"),"Historical fill falls outside a declared regular cash session; do not shift it to another date.");
    const firstIndex=eligible.indexOf(fills[0].day);
    if (firstIndex<1) return benchmarkUnavailable("A declared session before the first recorded fill is required as the cash and benchmark baseline.");
    const sessions=eligible.slice(firstIndex-1).slice(-253);
    const selectedKeys=new Set(fills.map(e=>historyKey({market:e.market,assetClass:e.asset_class,symbol:e.symbol})));
    BENCHMARKS.forEach(symbol=>selectedKeys.add(historyKey({market:"INDIA",assetClass:"INDEX",symbol})));
    const histories=new Map<string,Map<string,number>>(), providerIds=new Set<string>();
    for (const item of input.instruments.filter(i=>selectedKeys.has(historyKey(i)))) {
      requireValue(item.currency==="INR" && item.exchange==="NSE" && text(item.providerInstrumentId),"History currency, venue or provider identity does not match the recorded instrument or benchmark.");
      requireValue(!providerIds.has(item.providerInstrumentId),"Selected histories share a provider identity; instrument mapping needs review."); providerIds.add(item.providerInstrumentId);
      requireValue(Array.isArray(item.observations) && item.observations.length<=1000,"Instrument history exceeds the bounded observation limit.");
      requireValue(item.observations.every((o,i)=>o && declared.has(o.date) && finite(o.close) && o.close>0 && (!i || o.date>item.observations[i-1].date)),"Daily closes must be positive, unique, ordered and on completed declared sessions.");
      histories.set(historyKey(item),new Map(item.observations.map(o=>[o.date,o.close])));
    }
    let cash=validated.startingCapital, cursor=0;
    const quantities=new Map<string,{symbol:string;assetClass:string;quantity:number}>();
    const days:BenchmarkDay[]=sessions.map(date=>{
      let cashFees=0,fillCount=0;
      while (cursor<fills.length && fills[cursor].day<=date) {
        const e=fills[cursor++], k=historyKey({market:e.market,assetClass:e.asset_class,symbol:e.symbol}), fee=fees.get(e.order_id) ?? 0;
        const q=quantities.get(k) ?? {symbol:e.symbol,assetClass:e.asset_class,quantity:0};
        q.quantity+=(e.side==="BUY" ? 1 : -1)*e.quantity;
        cash+=(e.side==="BUY" ? -1 : 1)*Number(e.notional)-fee;
        requireValue(q.quantity>=0 && Number.isSafeInteger(q.quantity) && finite(cash),"Historical quantity or cash arithmetic is invalid.");
        quantities.set(k,q);
        if (e.day===date) {cashFees+=fee;fillCount++;}
      }
      const open=[...quantities.entries()].filter(([,q])=>q.quantity>0).sort(([a],[b])=>a.localeCompare(b));
      requireValue(open.length<=40,"Historical comparison supports at most 40 holdings on each session; no exposures are omitted.");
      const holdings=open.map(([k,q])=>{const close=histories.get(k)?.get(date) ?? null;return {...q,close,marketValue:close===null ? null : close*q.quantity};});
      requireValue(holdings.every(h=>h.marketValue===null || finite(h.marketValue)),"Historical marked value is nonfinite.");
      const missingHoldings=holdings.filter(h=>h.close===null).map(h=>`${h.symbol} · ${h.assetClass}`);
      const equity=missingHoldings.length ? null : cash+holdings.reduce((s,h)=>s+h.marketValue!,0);
      requireValue(equity===null || finite(equity) && equity>0,"Historical account equity must be positive and finite for return comparison.");
      return {date,cash,accountEquity:equity,cashFees,fillCount,holdings,missingHoldings,benchmarkCloses:Object.fromEntries(BENCHMARKS.map(symbol=>[symbol,histories.get(historyKey({market:"INDIA",assetClass:"INDEX",symbol}))?.get(date) ?? null])) as BenchmarkDay["benchmarkCloses"]};
    });
    const incomplete=days.some(d=>d.accountEquity===null || BENCHMARKS.some(b=>d.benchmarkCloses[b]===null));
    const report:AccountBenchmarkReport={schema:"pramana.account_benchmark.v1",qualification:"unqualified_price_comparison",tenantId,currency:"INR",ledgerId:validated.ledgerId,
      accountAsOf:validated.asOf,accountSha256:validated.sourceSha256,source:input.source,sourceAsOf:input.asOf,sourceSha256:digest({accountSha256:validated.sourceSha256,input}),
      calendarVersion:input.calendar.version,priceBasis:input.priceBasis,startingCapital:validated.startingCapital,historicalFillCount:fills.length,excludedLaterFillCount:entries.length-fills.length,
      benchmarks:BENCHMARKS.map(symbol=>({symbol,providerInstrumentId:input.instruments.find(i=>i.symbol===symbol && i.market==="INDIA" && i.assetClass==="INDEX")?.providerInstrumentId ?? null})),days,
      limitations:[
        "Historical marks of actual recorded paper fills, including closed positions, at provider daily closes. These are not live audited returns or current-holdings hypothetical returns.",
        "All recorded cash fees are expensed on their fill date. Spread and slippage already affect fill prices and are not deducted a second time. Unrecorded costs cannot be measured.",
        "NIFTY 50 and NIFTY BANK are nontradable price indices without dividends or execution costs; they are not investable net-return alternatives.",
        "INR cash equities/ETFs only. No external flows, dividends, splits, corporate actions, FX, financing or infrastructure costs. Source adjustments, mapping and calendar completeness are unqualified.",
        "Every held instrument must have a close on each selected session. No forward fill, skipped missing session, omitted exposure or inferred zero return.",
        "At most 253 session marks. Earlier recorded fills establish opening cash and holdings; results stop at the latest completed source session allowed by the account valuation.",
        "Relative risk uses the same adjacent daily returns, sample covariance with n−1 denominator and 252-session annualization. Twenty intervals is a display floor, not statistical proof.",
        "This report does not satisfy forward-paper strategy, runtime, provider or release-readiness gates. Local hashes identify inputs but do not authenticate them.",
      ]};
    return {status:incomplete ? "incomplete" : "available",detail:incomplete ? "Historical account/benchmark coverage has gaps. Select a fully covered range to calculate comparison statistics." : "Recorded paper account reconstructed at matched daily closes; source adjustments and investment performance remain unqualified.",report};
  } catch (error) {return {status:"invalid",detail:error instanceof DailyHistoryError ? error.message : "Account benchmark evidence could not be validated.",report:null};}
}
