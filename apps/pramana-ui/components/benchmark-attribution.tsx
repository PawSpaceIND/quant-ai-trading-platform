"use client";
import {useState} from "react";
import type {BenchmarkAttributionState} from "../lib/benchmark-attribution";
const pct=(value:string)=>(Number(value)*100).toFixed(3)+"%";
export function BenchmarkAttributionPanel({state,onAsk}:{state?:BenchmarkAttributionState;onAsk:(question:string)=>void}) {
  const [page,setPage]=useState(0);
  const r=state?.report;
  const current=r?Math.min(page,Math.max(0,Math.ceil(r.sectors.length/20)-1)):0;
  return <section className="panel research-comparison">
    <div className="panel-title"><h2>Benchmark return attribution</h2><span className="muted">Historical calculation</span></div>
    {!r?<p className="empty">{state?.detail||"No benchmark attribution report is configured."}</p>:<>
      <p>{r.portfolioId} versus {r.benchmarkId} · {r.periodStart} to {r.periodEnd} · INR</p>
      <p className="research-notice">{state?.detail} This is not current exposure, a profitability forecast or pilot approval.</p>
      <dl className="research-stats"><div><dt>Portfolio return</dt><dd>{pct(r.portfolioReturn)}</dd></div><div><dt>Benchmark return</dt><dd>{pct(r.benchmarkReturn)}</dd></div><div><dt>Active return</dt><dd>{pct(r.activeReturn)}</dd></div></dl>
      <p>Allocation {pct(r.totals.allocation)} · Selection {pct(r.totals.selection)} · Interaction {pct(r.totals.interaction)}</p>
      <div style={{overflowX:"auto"}}><table><caption>Sector effects in percentage points</caption><thead><tr><th scope="col">Sector</th><th scope="col">Allocation</th><th scope="col">Selection</th><th scope="col">Interaction</th><th scope="col">Total</th></tr></thead><tbody>{r.sectors.slice(current*20,current*20+20).map(s=><tr key={s.name}><th scope="row">{s.name}</th><td>{pct(s.allocation)}</td><td>{pct(s.selection)}</td><td>{pct(s.interaction)}</td><td>{pct(s.total)}</td></tr>)}</tbody></table></div>
      {r.sectors.length>20&&<div><button disabled={current===0} onClick={()=>setPage(current-1)}>Previous sectors</button><span> Page {current+1} of {Math.ceil(r.sectors.length/20)} </span><button disabled={(current+1)*20>=r.sectors.length} onClick={()=>setPage(current+1)}>Next sectors</button></div>}
      <a href="/api/research/attribution" download>Download attribution JSON</a>
      <button onClick={()=>onAsk(`Explain the historical benchmark attribution for ${r.portfolioId} versus ${r.benchmarkId} from ${r.periodStart} to ${r.periodEnd}. Distinguish calculation results from unverified source quality.`)}>Ask Atlas about attribution</button>
    </>}
  </section>;
}
