import fs from "node:fs";
import {parseBenchmarkAttribution, type BenchmarkAttributionState} from "./benchmark-attribution";

export function readBenchmarkAttribution(): BenchmarkAttributionState {
  const file=process.env.PRAMANA_BENCHMARK_ATTRIBUTION_REPORT;
  const portfolio=process.env.PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO;
  if(!file||!portfolio) return {status:"unavailable",detail:"No benchmark attribution report is configured.",report:null};
  try {
    const fd=fs.openSync(file,"r");
    let raw:string;
    try {const stat=fs.fstatSync(fd);if(!stat.isFile()||stat.size>4_000_000)throw Error("bound");raw=fs.readFileSync(fd,"utf8");} finally {fs.closeSync(fd);}
    const parsed=parseBenchmarkAttribution(raw,portfolio);
    const {input: _input,inputPayload: _payload,scope: _scope,...report}=parsed;
    return {status:"published",detail:"Calculated from supplied historical inputs. Source quality, income and cost treatment remain unverified.",report};
  } catch {return {status:"invalid",detail:"Attribution report is unreadable, invalid or does not match the selected portfolio.",report:null};}
}
export function benchmarkAttributionContext() {
  const state=readBenchmarkAttribution();
  return {...state,report:state.report?{...state.report,sectors:state.report.sectors.slice(0,30),totalSectorCount:state.report.sectors.length}:null};
}
