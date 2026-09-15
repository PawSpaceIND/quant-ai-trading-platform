"use client";
import type {ExternalAccountState} from "../lib/external-account";
const amount=(v:unknown)=>v===null?"Unavailable":new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2}).format(Number(v));
export function ExternalAccountPanel({state,onAsk}:{state?:ExternalAccountState;onAsk:(prompt:string)=>void}){
 const r=state?.report;
 return <section className="panel portfolio-replay" aria-label="Selected external account observation">
  <div className="panel-title"><div><span className="eyebrow">EXTERNAL BROKER · READ ONLY</span><h2>Selected account funds &amp; net positions</h2></div><span className="pill amber">{state?.status==="available"?"Observed":state?.status==="stale"?"Historical":"Unavailable"}</span></div>
  {!r?<p className="empty">{state?.detail||"No selected account observation is configured."}</p>:<>
   <p>{state?.detail} Captured {new Date(r.finishedAt).toLocaleString()} · Repeated reads: {r.status} · {r.positions.length} net positions</p>
   {r.status!=="consistent"&&<p className="research-notice">Repeated reads changed or required funds are missing. Current account consistency is withheld.</p>}
   <dl className="research-stats"><div><dt>Cash balance</dt><dd>{amount(r.funds.cash_balance)}</dd></div><div><dt>Available balance</dt><dd>{amount(r.funds.available_balance)}</dd></div><div><dt>Net trading funds</dt><dd>{amount(r.funds.net_trading_funds)}</dd></div></dl>
   <div style={{overflowX:"auto"}}><table><caption>Broker net positions; depository holdings are excluded</caption><thead><tr><th scope="col">Instrument</th><th scope="col">Exchange</th><th scope="col">Product</th><th scope="col">Quantity</th><th scope="col">Average cost</th></tr></thead><tbody>{r.positions.slice(0,100).map((p,i)=><tr key={`${p.instrument_id}-${p.exchange}-${p.product}-${i}`}><th scope="row">{String(p.symbol)}</th><td>{String(p.exchange)}</td><td>{String(p.product)}</td><td>{String(p.quantity)}</td><td>{amount(p.average_cost)}</td></tr>)}</tbody></table></div>
   {r.positions.length>100&&<p>Showing 100 of {r.positions.length} positions. Download the full private report.</p>}
   <p className="footnote">This selected external account is separate from the paper ledger. No paper cash or position parity, complete holdings inventory, settlement or launch approval is established.</p>
   <a href="/api/broker/external-account" download>Download selected account JSON</a>
   <button onClick={()=>onAsk(`Explain the selected external account snapshot captured ${r.finishedAt}: status ${r.status}, ${r.positions.length} net positions. Distinguish it from the separate paper ledger and state that depository holdings and cash/position reconciliation are unverified.`)}>Ask Atlas about this account</button>
  </>}
 </section>;
}
