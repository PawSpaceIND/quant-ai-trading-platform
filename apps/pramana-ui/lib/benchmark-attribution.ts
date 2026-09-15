import {createHash} from "node:crypto";

export type BenchmarkAttribution = {
  schema: "pramana.benchmark_attribution.v1"; method: "single_period_brinson_fachler";
  portfolioId: string; benchmarkId: string; periodStart: string; periodEnd: string;
  currency: "INR"; sourceQualified: false; portfolioReturn: string; benchmarkReturn: string;
  activeReturn: string; reconciliationDifference: string;
  sectors: {name: string; allocation: string; selection: string; interaction: string; total: string}[];
  totals: {allocation: string; selection: string; interaction: string; total: string};
  inputSha256: string; inputPayload: string; input: unknown; scope: string;
};
function check(value: unknown): asserts value {if (!value) throw Error("invalid_benchmark_attribution");}
function decimal(value: unknown): number {
  check(typeof value === "string" && value.length <= 80 && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value));
  const n = Number(value); check(Number.isFinite(n) && Math.abs(n) <= 1000000); return n;
}
function same(value: unknown, expected: number) {check(Math.abs(decimal(value)-expected) <= 1e-12*Math.max(1,Math.abs(expected)));}
function label(value: unknown): asserts value is string {check(typeof value === "string" && value.trim() === value && value.length > 0 && value.length <= 100 && !/[\x00-\x1f\x7f]/.test(value));}
function day(value: unknown): asserts value is string {
  check(typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value));
  const time = Date.parse(value); check(Number.isFinite(time) && new Date(time).toISOString().slice(0,10) === value);
}

/** Independent arithmetic check with 1e-12 floating tolerance; not source certification. */
export function parseBenchmarkAttribution(raw: string, portfolioId: string): BenchmarkAttribution {
  check(Buffer.byteLength(raw) <= 4_000_000);
  const r = JSON.parse(raw);
  check(r && r.schema === "pramana.benchmark_attribution.v1" && r.method === "single_period_brinson_fachler" && r.currency === "INR" && r.sourceQualified === false);
  check(typeof r.inputPayload === "string" && Buffer.byteLength(r.inputPayload) <= 1_000_000);
  check(createHash("sha256").update(r.inputPayload).digest("hex") === r.inputSha256);
  const input = JSON.parse(r.inputPayload);
  check(JSON.stringify(input) === JSON.stringify(r.input));
  check(input.schema === "pramana.benchmark_attribution_input.v1" && input.basis === "beginning_weights_same_period_total_returns" && input.currency === "INR");
  for (const key of ["portfolioId","benchmarkId"] as const) {label(r[key]); check(input[key] === r[key]);}
  check(r.portfolioId === portfolioId);
  day(r.periodStart); day(r.periodEnd); check(r.periodStart < r.periodEnd);
  check(input.periodStart === r.periodStart && input.periodEnd === r.periodEnd);
  check(Array.isArray(input.sectors) && input.sectors.length > 0 && input.sectors.length <= 500);
  const names = new Set<string>();
  const rows = input.sectors.map((s: Record<string, unknown>) => {
    check(s && typeof s === "object"); label(s.name); check(!names.has(s.name)); names.add(s.name);
    const wp=decimal(s.portfolioWeight), wb=decimal(s.benchmarkWeight), rp=decimal(s.portfolioReturn), rb=decimal(s.benchmarkReturn);
    check(wp>=0 && wp<=1 && wb>=0 && wb<=1 && rp>=-1 && rb>=-1 && rp<=1000 && rb<=1000);
    return {name:s.name,wp,wb,rp,rb};
  }) as {name:string;wp:number;wb:number;rp:number;rb:number}[];
  same(String(rows.reduce((n,s)=>n+s.wp,0)),1); same(String(rows.reduce((n,s)=>n+s.wb,0)),1);
  const pr=rows.reduce((n,s)=>n+s.wp*s.rp,0), br=rows.reduce((n,s)=>n+s.wb*s.rb,0);
  same(input.portfolioReturn,pr); same(input.benchmarkReturn,br); same(r.portfolioReturn,pr); same(r.benchmarkReturn,br);
  check(Array.isArray(r.sectors) && r.sectors.length === rows.length);
  const totals={allocation:0,selection:0,interaction:0,total:0};
  rows.forEach((s,i)=>{
    const allocation=(s.wp-s.wb)*(s.rb-br), selection=s.wb*(s.rp-s.rb), interaction=(s.wp-s.wb)*(s.rp-s.rb);
    const values={allocation,selection,interaction,total:allocation+selection+interaction};
    check(r.sectors[i]?.name===s.name);
    for(const key of Object.keys(values) as (keyof typeof values)[]) {same(r.sectors[i][key],values[key]);totals[key]+=values[key];}
  });
  for(const key of Object.keys(totals) as (keyof typeof totals)[]) same(r.totals?.[key],totals[key]);
  same(r.activeReturn,pr-br); same(r.reconciliationDifference,0); same(String(totals.total),pr-br);
  check(typeof r.scope === "string" && r.scope.length <= 1000);
  return r as BenchmarkAttribution;
}

export type BenchmarkAttributionSummary = Omit<BenchmarkAttribution,"input"|"inputPayload"|"scope">;
export type BenchmarkAttributionState = {status:"unavailable"|"invalid"|"published";detail:string;report:BenchmarkAttributionSummary|null};
