import {test} from "node:test";
import assert from "node:assert/strict";
import {parseBenchmarkAttribution} from "../lib/benchmark-attribution";
import fs from "node:fs";
const valid=JSON.parse(fs.readFileSync(new URL("./fixtures/benchmark-attribution.json",import.meta.url),"utf8"));
test("Python report independently reconciles in UI reader",()=>{assert.equal(parseBenchmarkAttribution(JSON.stringify(valid),"example-portfolio").sourceQualified,false);});
test("altered report, source and portfolio are rejected",()=>{
 assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(valid),"other"));
 for(const field of ["inputPayload","activeReturn","periodEnd"]){const r=structuredClone(valid);r[field]="invalid";assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(r),"example-portfolio"));}
 const r=structuredClone(valid);r.sectors[0].allocation=".99";assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(r),"example-portfolio"));
});

test("configured reader withholds mismatched portfolio and omits raw inputs",async()=>{
 const {readBenchmarkAttribution}=await import("../lib/benchmark-attribution-store");
 const priorFile=process.env.PRAMANA_BENCHMARK_ATTRIBUTION_REPORT, priorPortfolio=process.env.PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO;
 try {
  delete process.env.PRAMANA_BENCHMARK_ATTRIBUTION_REPORT;assert.equal(readBenchmarkAttribution().report,null);
  process.env.PRAMANA_BENCHMARK_ATTRIBUTION_REPORT=new URL("./fixtures/benchmark-attribution.json",import.meta.url).pathname;
  process.env.PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO="example-portfolio";
  const state=readBenchmarkAttribution();assert.equal(state.status,"published");assert(!Object.hasOwn(state.report!,"inputPayload"));assert(!Object.hasOwn(state.report!,"input"));
  process.env.PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO="other";assert.equal(readBenchmarkAttribution().report,null);
 } finally {
  if(priorFile===undefined)delete process.env.PRAMANA_BENCHMARK_ATTRIBUTION_REPORT;else process.env.PRAMANA_BENCHMARK_ATTRIBUTION_REPORT=priorFile;
  if(priorPortfolio===undefined)delete process.env.PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO;else process.env.PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO=priorPortfolio;
 }
});


test("unexpected report fields cannot cross into workspace summaries",()=>{
 for(const target of ["root","sector","totals"]){
  const report=structuredClone(valid);
  const object=target==="root"?report:target==="sector"?report.sectors[0]:report.totals;
  object.privateReviewerNotes="not for workspace";
  assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(report),"example-portfolio"));
 }
});
