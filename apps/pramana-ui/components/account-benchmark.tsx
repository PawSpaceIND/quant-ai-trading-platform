"use client";
import {useMemo, useState} from "react";
import {CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis} from "recharts";
import {BENCHMARKS, LOOKBACKS, compareBenchmark, type AccountBenchmarkState, type Benchmark, type Lookback} from "../lib/benchmark-comparison";

const currency=new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2});
const money=(n:number|null)=>n===null ? "Unavailable" : currency.format(n);
const pct=(n:number|null)=>n===null ? "Unavailable" : `${(n*100).toFixed(3)}%`;
const number=(n:number|null)=>n===null ? "Undefined" : n.toFixed(3);

export function AccountBenchmark({state,onAsk}:{state?:AccountBenchmarkState;onAsk:(prompt:string)=>void}) {
  const [benchmark,setBenchmark]=useState<Benchmark>("NIFTY 50");
  const [lookback,setLookback]=useState<Lookback>(60);
  const [selectedDate,setSelectedDate]=useState("");
  const [page,setPage]=useState(0);
  const report=state?.report;
  const result=useMemo(()=>report ? compareBenchmark(report,benchmark,lookback) : null,[report,benchmark,lookback]);
  const currentPage=Math.min(page,Math.max(0,Math.ceil((result?.days.length ?? 0)/10)-1));
  const selected=result?.days.find(d=>d.date===selectedDate) ?? result?.days.at(-1);
  return <section className="panel portfolio-replay account-benchmark">
    <div className="panel-title"><div><span className="eyebrow">RECORDED PAPER ACCOUNT · DAILY CLOSES</span><h2>Account versus benchmark</h2></div><span className="pill neutral">{state?.status ?? "unavailable"}</span></div>
    <p className="research-notice">{state?.detail ?? "A reconciled account and completed daily history are required."}</p>
    <p className="muted">Actual recorded paper trades and cash fees, valued at each day's provider close. Benchmark returns exclude dividends and trading costs. Source adjustments are unverified.</p>
    {report && result ? <>
      <div className="replay-selectors"><label>Price benchmark<select aria-label="Account price benchmark" value={benchmark} onChange={e=>setBenchmark(e.target.value as Benchmark)}>{BENCHMARKS.map(b=><option key={b}>{b}</option>)}</select></label>
        <label>Comparison window<select aria-label="Account comparison window" value={lookback} onChange={e=>{setLookback(Number(e.target.value) as Lookback);setPage(0);}}>{LOOKBACKS.map(n=><option key={n} value={n}>Up to {n} sessions</option>)}</select></label></div>
      <p className="footnote">{result.from}–{result.through} · {result.intervals} adjacent-session intervals · {report.excludedLaterFillCount} later fills excluded · Account ledger #{report.ledgerId}</p>
      {!result.complete ? <p className="research-notice" role="status">Comparison withheld: {result.missingAccountDates.length} account dates and {result.missingBenchmarkDates.length} {benchmark} dates lack evidence. No missing close is replaced or skipped.</p> : null}
      <dl className="research-stats replay-metrics">
        <div><dt>Paper account after recorded fees</dt><dd>{pct(result.accountReturn)}</dd></div>
        <div><dt>{benchmark} price return</dt><dd>{pct(result.benchmarkReturn)}</dd></div>
        <div><dt>Excess return (percentage points)</dt><dd>{result.excessReturn===null ? "Unavailable" : (result.excessReturn*100).toFixed(3)}</dd></div>
        <div><dt>Relative wealth return</dt><dd>{pct(result.relativeWealthReturn)}</dd></div>
        <div><dt>Account max daily drawdown</dt><dd>{pct(result.accountDrawdown)}</dd></div>
        <div><dt>Benchmark max daily drawdown</dt><dd>{pct(result.benchmarkDrawdown)}</dd></div>
        <div><dt>Annualized tracking error</dt><dd>{pct(result.trackingError)}</dd></div>
        <div><dt>Annualized information ratio</dt><dd>{number(result.informationRatio)}</dd></div>
        <div><dt>Beta to {benchmark}</dt><dd>{number(result.beta)}</dd></div>
        <div><dt>Daily return correlation</dt><dd>{number(result.correlation)}</dd></div>
      </dl>
      <p className="footnote">{result.statisticsReason} Excess return subtracts period returns; relative wealth divides their growth factors. Daily drawdown can miss intraday losses.</p>
      <div style={{height:260,minWidth:0}} role="img" aria-label={`Paper account and ${benchmark} daily values rebased to 100; missing closes leave gaps. Exact values are in the daily table.`}>
        <ResponsiveContainer width="100%" height="100%"><LineChart data={result.curve} margin={{left:0,right:16,top:12,bottom:4}}><CartesianGrid strokeDasharray="3 3" stroke="#27394b"/><XAxis dataKey="date" minTickGap={50}/><YAxis domain={["auto","auto"]} width={54}/><Tooltip/><Legend/><Line name="Paper account (base 100)" dataKey="account" stroke="#48c9b0" dot={false} connectNulls={false} isAnimationActive={false}/><Line name={`${benchmark} price (base 100)`} dataKey="benchmark" stroke="#8d9dff" dot={false} connectNulls={false} isAnimationActive={false}/></LineChart></ResponsiveContainer>
      </div>
      <p className="footnote">Both curves start at 100 on the first selected date when its value exists. A missing baseline withholds that entire rebased curve.</p>
      <details><summary>Inspect daily account and benchmark evidence</summary>
        <div className="research-table-scroll" role="region" tabIndex={0} aria-label="Account benchmark daily values"><table><caption>Recorded end-of-session book, marked at provider closes</caption><thead><tr><th scope="col">Session</th><th scope="col">Account equity</th><th scope="col">Cash</th><th scope="col">{benchmark} close</th><th scope="col">Cash fees</th><th scope="col">Fills</th><th scope="col">Missing holdings</th></tr></thead><tbody>{result.days.slice(currentPage*10,currentPage*10+10).map(d=><tr key={d.date}><th scope="row"><button aria-pressed={selected?.date===d.date} onClick={()=>setSelectedDate(d.date)}>{d.date}</button></th><td>{money(d.accountEquity)}</td><td>{money(d.cash)}</td><td>{d.benchmarkCloses[benchmark]?.toFixed(2) ?? "Unavailable"}</td><td>{money(d.cashFees)}</td><td>{d.fillCount}</td><td>{d.missingHoldings.join(", ") || "None"}</td></tr>)}</tbody></table></div>
        <div className="replay-pagination"><span>{currentPage*10+1}–{Math.min(currentPage*10+10,result.days.length)} of {result.days.length}</span><button aria-label="Previous account benchmark days" disabled={!currentPage} onClick={()=>setPage(currentPage-1)}>Previous</button><button aria-label="Next account benchmark days" disabled={(currentPage+1)*10>=result.days.length} onClick={()=>setPage(currentPage+1)}>Next</button></div>
        {selected ? <><h3>Recorded holdings on {selected.date}</h3>{selected.holdings.length ? <div className="research-table-scroll" role="region" tabIndex={0} aria-label={`Recorded holdings on ${selected.date}`}><table><thead><tr><th scope="col">Holding</th><th scope="col">Quantity</th><th scope="col">Daily close</th><th scope="col">Market value</th></tr></thead><tbody>{selected.holdings.map(h=><tr key={`${h.assetClass}:${h.symbol}`}><th scope="row">{h.symbol} · {h.assetClass}</th><td>{h.quantity}</td><td>{money(h.close)}</td><td>{money(h.marketValue)}</td></tr>)}</tbody></table></div> : <p>No holdings at this session close; cash {money(selected.cash)}.</p>}</> : null}
      </details>
      <details><summary>Comparison method and source limits</summary><p>{report.source} · Captured {new Date(report.sourceAsOf).toLocaleString()} · {report.calendarVersion}</p><p>Account evidence as of {new Date(report.accountAsOf).toLocaleString()} · Historical fills {report.historicalFillCount}</p><ul>{report.limitations.map(l=><li key={l}>{l}</li>)}</ul><p className="footnote">Source SHA-256: <code>{report.sourceSha256}</code></p><p className="footnote">Account SHA-256: <code>{report.accountSha256}</code></p></details>
      <div className="research-actions"><button onClick={()=>onAsk(`Explain the recorded paper-account comparison against ${benchmark} for up to ${lookback} sessions. Use the matching benchmarkPerformance comparison in the saved context, including actual interval count, coverage gaps, net recorded fees, excess versus relative wealth return, drawdown, tracking error and beta. Preserve the unqualified provider-close and price-index limitations. Do not treat this as live performance, strategy qualification or a forecast.`)}>Discuss comparison with Atlas ↗</button><a href={`/api/portfolio/benchmark?benchmark=${encodeURIComponent(benchmark)}&lookback=${lookback}`} download="pramana-account-benchmark.json">Export selected comparison ↓</a></div>
    </> : null}
  </section>;
}
