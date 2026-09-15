"use client";
import {useState} from "react";
import type {HistoricalRiskState} from "../lib/historical-risk";
const currency=new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2});
const money = (n:number|null) => n === null ? "Unavailable" : currency.format(n);
const pct = (n:number|null) => n === null ? "Unavailable" : `${(n*100).toFixed(3)}%`;

export function HistoricalRisk({state,onAsk}:{state?:HistoricalRiskState;onAsk:(prompt:string)=>void}) {
  const [selected,setSelected]=useState("");
  const [sort,setSort]=useState("risk");
  const [page,setPage]=useState(0);
  const report=state?.report;
  const selectedIndex=Math.max(0,report?.rows.findIndex(r=>r.key===selected) ?? 0);
  const rows=[...(report?.rows ?? state?.rows ?? [])].sort((a,b)=>sort==="symbol" ? a.symbol.localeCompare(b.symbol) : (b.varianceShare ?? -Infinity)-(a.varianceShare ?? -Infinity) || a.symbol.localeCompare(b.symbol));
  const currentPage=Math.min(page,Math.max(0,Math.ceil((report?.scenarios.length ?? 0)/10)-1));
  return <section className="panel portfolio-replay historical-risk">
    <div className="panel-title"><div><span className="eyebrow">HISTORICAL RISK · EXPLORATORY</span><h2>How holdings move together</h2></div><span className="pill neutral">{state?.status ?? "unavailable"}</span></div>
    <p className="research-notice">{state?.detail ?? "Completed, source-labelled daily history is required."}</p>
    <p className="muted">Reprice today's holdings using past daily moves. These estimates are not forecasts or maximum-loss limits. Corporate-action adjustments and source mapping remain unverified.</p>
    {report ? <>
      <p className="footnote">{report.from}–{report.through} · {report.intervals} complete adjacent-session intervals · Cash {pct(report.cashWeight)} of equity · Valuation {new Date(report.valuationAsOf).toLocaleString()}</p>
      <dl className="research-stats replay-metrics">
        <div><dt>Historical daily volatility</dt><dd>{pct(report.dailyVolatility)}</dd></div>
        <div><dt>95% historical VaR loss</dt><dd>{money(report.historicalLoss.var)}</dd></div>
        <div><dt>95% expected shortfall loss</dt><dd>{money(report.historicalLoss.expectedShortfall)}</dd></div>
        <div><dt>Worst observed repricing loss</dt><dd>{money(report.historicalLoss.worst)}</dd></div>
        <div><dt>Historical diversification ratio</dt><dd>{report.diversificationRatio?.toFixed(3) ?? "Unavailable"}</dd></div>
      </dl>
      {report.historicalLoss.reason && <p className="research-notice">{report.historicalLoss.reason}</p>}
      <p className="footnote">Losses use current equity of {money(report.totalEquity)} and fixed holdings. Negative loss values mean gains in the historical sample. Expected shortfall uses {report.historicalLoss.tailMass.toFixed(2)} equivalent observations in the worst 5%; it does not cover every possible tail event.</p>
      <div className="replay-selectors"><label>Contribution order<select aria-label="Historical risk contribution order" value={sort} onChange={e=>setSort(e.target.value)}><option value="risk">Highest variance contribution</option><option value="symbol">Symbol</option></select></label>
      <label>Inspect correlations for<select aria-label="Correlation holding" value={report.rows[selectedIndex]?.key ?? ""} onChange={e=>setSelected(e.target.value)}>{report.rows.map(r=><option value={r.key} key={r.key}>{r.symbol} · {r.assetClass}</option>)}</select></label></div>
      <div className="research-table-scroll" role="region" tabIndex={0} aria-label="Historical risk contributions"><table><caption>Current holdings under historical daily moves</caption><thead><tr><th scope="col">Holding</th><th scope="col">Equity weight</th><th scope="col">Daily volatility</th><th scope="col">Volatility contribution (pp)</th><th scope="col">Share of variance</th><th scope="col">Correlation with {report.rows[selectedIndex]?.symbol}</th></tr></thead><tbody>{rows.map(r=>{const index=report.rows.findIndex(x=>x.key===r.key);return <tr key={r.key}><th scope="row">{r.symbol}<span className="muted"> · {r.assetClass}</span></th><td>{pct(r.equityWeight)}</td><td>{pct(r.dailyVolatility)}</td><td>{r.volatilityContribution===null ? "Unavailable" : (r.volatilityContribution*100).toFixed(4)}</td><td>{pct(r.varianceShare)}</td><td>{report.correlation[selectedIndex][index]?.toFixed(3) ?? "Undefined (constant series)"}</td></tr>;})}</tbody></table></div>
      <p className="footnote">Contributions sum to portfolio volatility/variance when defined. Negative contributions can occur for offsets. Undefined correlations are not zero correlation. Diversification ratio compares weighted standalone volatility to joint volatility; it is not a target or guarantee.</p>
      <details><summary>Historical loss scenarios</summary><p>Largest repricing losses first. This is today's exposure under past returns, not trades or account performance on those dates.</p><div className="research-table-scroll" role="region" tabIndex={0} aria-label="Historical repricing scenarios"><table><thead><tr><th scope="col">From</th><th scope="col">Through</th><th scope="col">Repricing P&amp;L</th><th scope="col">Equity impact</th></tr></thead><tbody>{report.scenarios.slice(currentPage*10,currentPage*10+10).map(s=><tr key={s.through}><td>{s.from}</td><td>{s.through}</td><td>{money(s.pnl)}</td><td>{pct(s.equityReturn)}</td></tr>)}</tbody></table></div><div className="replay-pagination"><span>{currentPage*10+1}–{Math.min(currentPage*10+10,report.scenarios.length)} of {report.scenarios.length}</span><button disabled={!currentPage} onClick={()=>setPage(currentPage-1)} aria-label="Previous historical risk scenarios">Previous</button><button disabled={(currentPage+1)*10>=report.scenarios.length} onClick={()=>setPage(currentPage+1)} aria-label="Next historical risk scenarios">Next</button></div></details>
      <details><summary>Risk method and source limits</summary><p>{report.source} · Captured {new Date(report.sourceAsOf).toLocaleString()} · {report.calendarVersion}</p><ul>{report.limitations.map(l=><li key={l}>{l}</li>)}</ul><p className="footnote">Source SHA-256: <code>{report.sourceSha256}</code></p></details>
      <div className="research-actions"><button onClick={()=>onAsk("Explain the current exploratory historical risk report: covariance and correlation, instrument volatility contributions, 95% historical VaR/expected shortfall and worst repricing scenarios. Preserve sample size, source/calendar/adjustment limitations and cash weights. These are current-holding hypothetical shocks, not realized strategy results or a loss cap. Do not recommend sizing or promise future protection from these unqualified estimates.")}>Discuss historical risk with Atlas ↗</button><a href="/api/portfolio/historical-risk" download="pramana-historical-risk.json">Export historical risk ↓</a></div>
    </> : rows.length>0 && <div className="research-table-scroll" role="region" tabIndex={0} aria-label="Risk history coverage"><table><caption>Missing history is not treated as zero risk</caption><thead><tr><th scope="col">Holding</th><th scope="col">Closes present</th><th scope="col">Missing session closes</th><th scope="col">First missing date</th></tr></thead><tbody>{rows.map(r=><tr key={r.key}><th scope="row">{r.symbol}</th><td>{r.observations}</td><td>{r.missingDates.length}</td><td>{r.missingDates[0] ?? "None"}</td></tr>)}</tbody></table></div>}
  </section>;
}
