/** Browser-safe calculations shared by the dashboard, export and Atlas context. */
export const BENCHMARKS = ["NIFTY 50", "NIFTY BANK"] as const;
export const LOOKBACKS = [20, 60, 252] as const;
export type Benchmark = typeof BENCHMARKS[number];
export type Lookback = typeof LOOKBACKS[number];
export type BenchmarkDay = {
  date: string; cash: number; accountEquity: number | null; cashFees: number; fillCount: number;
  holdings: {symbol: string; assetClass: string; quantity: number; close: number | null; marketValue: number | null}[];
  missingHoldings: string[]; benchmarkCloses: Record<Benchmark, number | null>;
};
export type AccountBenchmarkReport = {
  schema: "pramana.account_benchmark.v1"; qualification: "unqualified_price_comparison";
  tenantId: string; currency: "INR"; ledgerId: number; accountAsOf: string; accountSha256: string;
  source: string; sourceAsOf: string; sourceSha256: string; calendarVersion: string; priceBasis: string;
  startingCapital: number; historicalFillCount: number; excludedLaterFillCount: number;
  benchmarks: {symbol: Benchmark; providerInstrumentId: string | null}[];
  days: BenchmarkDay[]; limitations: string[];
};
export type AccountBenchmarkState = {
  status: "available" | "incomplete" | "unavailable" | "invalid"; detail: string; report: AccountBenchmarkReport | null;
};
const mean = (xs: number[]) => xs.reduce((a,b) => a+b,0)/xs.length;
const drawdown = (xs: number[]) => {let peak=xs[0], worst=0; for (const x of xs) {peak=Math.max(peak,x); worst=Math.max(worst,1-x/peak);} return worst;};
const safe = (n: number) => Number.isFinite(n) ? n : null;

export function compareBenchmark(report: AccountBenchmarkReport, benchmark: Benchmark, lookback: Lookback) {
  const days = report.days.slice(-lookback-1), first = days[0], last = days.at(-1);
  const missingAccountDates = days.filter(d=>d.accountEquity===null).map(d=>d.date);
  const missingBenchmarkDates = days.filter(d=>d.benchmarkCloses[benchmark]===null).map(d=>d.date);
  const intervals = Math.max(0,days.length-1);
  const complete = intervals>0 && !missingAccountDates.length && !missingBenchmarkDates.length;
  const a0=first?.accountEquity, b0=first?.benchmarkCloses[benchmark];
  const curve = days.map(d=>({date:d.date,
    account: a0 && d.accountEquity !== null ? safe(100*d.accountEquity/a0) : null,
    benchmark: b0 && d.benchmarkCloses[benchmark] !== null ? safe(100*d.benchmarkCloses[benchmark]!/b0) : null}));
  let accountReturn:number|null=null, benchmarkReturn:number|null=null, excessReturn:number|null=null, relativeWealthReturn:number|null=null;
  let accountDrawdown:number|null=null, benchmarkDrawdown:number|null=null;
  let trackingError:number|null=null, informationRatio:number|null=null, beta:number|null=null, correlation:number|null=null;
  let statisticsReason = "At least 20 complete adjacent-session intervals are required for relative-risk statistics.";
  if (!complete) statisticsReason = "Incomplete selected range: comparison statistics are withheld; missing dates are not skipped or filled.";
  if (complete) {
    const a=days.map(d=>d.accountEquity!), b=days.map(d=>d.benchmarkCloses[benchmark]!);
    accountReturn=safe(a.at(-1)!/a[0]-1); benchmarkReturn=safe(b.at(-1)!/b[0]-1);
    if (accountReturn!==null && benchmarkReturn!==null) {
      excessReturn=safe(accountReturn-benchmarkReturn);
      relativeWealthReturn=safe((1+accountReturn)/(1+benchmarkReturn)-1);
    }
    accountDrawdown=drawdown(a); benchmarkDrawdown=drawdown(b);
    if (intervals>=20) {
      const ar=a.slice(1).map((x,i)=>x/a[i]-1), br=b.slice(1).map((x,i)=>x/b[i]-1), active=ar.map((x,i)=>x-br[i]);
      const am=mean(ar), bm=mean(br), dm=mean(active);
      const av=ar.reduce((s,x)=>s+(x-am)**2,0)/(intervals-1), bv=br.reduce((s,x)=>s+(x-bm)**2,0)/(intervals-1);
      const dv=active.reduce((s,x)=>s+(x-dm)**2,0)/(intervals-1), cov=ar.reduce((s,x,i)=>s+(x-am)*(br[i]-bm),0)/(intervals-1);
      trackingError=safe(Math.sqrt(dv*252));
      informationRatio=dv>1e-24 ? safe(dm/Math.sqrt(dv)*Math.sqrt(252)) : null;
      beta=bv>1e-24 ? safe(cov/bv) : null;
      correlation=av>1e-24 && bv>1e-24 ? safe(Math.max(-1,Math.min(1,cov/Math.sqrt(av*bv)))) : null;
      statisticsReason = [trackingError,informationRatio,beta,correlation].some(n=>n===null)
        ? "Constant series or nonfinite arithmetic leaves some statistics undefined; undefined values are not zero." : "Exploratory daily statistics, annualized with 252 sessions; this sample does not qualify a strategy.";
    }
  }
  return {benchmark,lookback,from:first?.date ?? null,through:last?.date ?? null,intervals,complete,
    missingAccountDates,missingBenchmarkDates,accountReturn,benchmarkReturn,excessReturn,relativeWealthReturn,
    accountDrawdown,benchmarkDrawdown,trackingError,informationRatio,beta,correlation,statisticsReason,curve,days};
}

export function accountBenchmarkContext(state: AccountBenchmarkState) {
  if (!state.report) return state;
  const {days,...report}=state.report;
  return {...state,report:{...report,omittedDailyRows:days.length,
    comparisons:BENCHMARKS.flatMap(b=>LOOKBACKS.map(l=>{
      const {days:selectedDays,curve,...comparison}=compareBenchmark(state.report!,b,l);
      return {...comparison,omittedDailyRows:selectedDays.length,omittedCurvePoints:curve.length};
    }))}};
}
