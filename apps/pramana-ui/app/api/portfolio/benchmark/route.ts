import {NextResponse} from "next/server";
import {readPortfolioSnapshot} from "@/lib/portfolio";
import {readMarket} from "@/lib/market";
import {BENCHMARKS, LOOKBACKS, compareBenchmark, type Benchmark, type Lookback} from "@/lib/benchmark-comparison";

export const dynamic="force-dynamic";
export async function GET(request:Request) {
  const query=new URL(request.url).searchParams;
  const benchmark=query.get("benchmark") ?? "NIFTY 50", lookback=query.get("lookback") ?? "60";
  if (query.getAll("benchmark").length>1 || query.getAll("lookback").length>1 || !BENCHMARKS.includes(benchmark as Benchmark) || !LOOKBACKS.some(n=>String(n)===lookback))
    return NextResponse.json({error:"Choose NIFTY 50 or NIFTY BANK and a 20, 60 or 252 session window."},{status:400,headers:{"Cache-Control":"no-store"}});
  try {
    const market=await readMarket(), state=readPortfolioSnapshot(market.riskHistory).benchmarkPerformance;
    return new NextResponse(JSON.stringify({...state,comparison:state.report ? compareBenchmark(state.report,benchmark as Benchmark,Number(lookback) as Lookback) : null},null,2),
      {status:state.report ? 200 : state.status==="invalid" ? 503 : 422,headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store","X-Content-Type-Options":"nosniff",...(state.report ? {"Content-Disposition":'attachment; filename="pramana-account-benchmark.json"'} : {})}});
  } catch {return NextResponse.json({error:"Account benchmark evidence is unavailable."},{status:503,headers:{"Cache-Control":"no-store"}});}
}
