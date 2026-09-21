"use client";
import {useEffect,useState} from "react";
import type {BrokerSelection} from "@/lib/broker-lifecycle";
import type {BrokerObservationState} from "@/lib/broker-observation";

function BrokerObservationBody({state,onAsk}:{state?:BrokerObservationState;onAsk:(prompt:string)=>void}) {
  const [query,setQuery]=useState(""),[filter,setFilter]=useState("all"),[page,setPage]=useState(0),[selected,setSelected]=useState<string|null>(null);
  const report=state?.report,inspection=state?.inspection;
  const rows=(report?.orders||[]).filter(o=>(filter==="all"||o.status===filter)&&`${o.symbol} ${o.orderId} ${o.exchange} ${o.product}`.toLowerCase().includes(query.toLowerCase()));
  const maxPage=Math.max(0,Math.ceil(rows.length/10)-1), currentPage=Math.min(page,maxPage);
  const chosen=rows.find(o=>o.orderId===selected),trades=chosen?report?.trades.filter(t=>t.orderId===chosen.orderId):[];
  return <section className="panel portfolio-replay" aria-label="Broker order and trade observation">
    <div className="panel-title"><div><span className="eyebrow">EXTERNAL BROKER · READ ONLY</span><h2>Broker order &amp; trade checks</h2></div><span className="pill amber">{state?.status==="stale"?"Historical":state?.status==="available"?inspection?.status||"Unverified":"Unverified"}</span></div>
    <p className="research-notice">{state?.detail||"No external broker observation is configured."}</p>
    {report&&inspection&&<>
      <p className="muted">Kite · account reference {report.accountRef.slice(0,12)} · captured {new Date(report.finishedAt).toLocaleString()}. Capture window {((Date.parse(report.finishedAt)-Date.parse(report.startedAt))/1000).toFixed(1)}s.</p>
      <dl className="research-stats replay-metrics"><div><dt>Orders</dt><dd>{inspection.orderCount}</dd></div><div><dt>Executions</dt><dd>{inspection.tradeCount}</dd></div><div><dt>Nonterminal orders</dt><dd>{inspection.openOrderCount}</dd></div><div><dt>Observed issues</dt><dd>{inspection.issueCount}</dd></div></dl>
      {inspection.status==="changing"&&<p className="research-notice">Orders or trades changed between reads. Consistency is withheld; capture again after updates settle.</p>}
      {inspection.status==="empty"&&<p className="research-notice">No orders or trades were returned. Empty evidence does not qualify broker lifecycle handling.</p>}
      {!!inspection.issues.length&&<details open><summary>Inspect issues ({inspection.issues.length} of {inspection.issueCount})</summary><ul>{inspection.issues.map((i,n)=><li key={n}>{i.code.replaceAll("_"," ")}{i.orderId?` · ${i.orderId}`:""}</li>)}</ul></details>}
      <div className="replay-selectors"><label>Search broker orders<input aria-label="Search broker orders" value={query} placeholder="Symbol, order, exchange or product" onChange={e=>{setQuery(e.target.value);setPage(0);}}/></label><label>Status<select aria-label="Broker order status" value={filter} onChange={e=>{setFilter(e.target.value);setPage(0);}}><option value="all">All statuses</option>{[...new Set(report.orders.map(o=>o.status))].sort().map(s=><option key={s}>{s}</option>)}</select></label></div><div className="research-actions"><a href={report&&state?.journal?`/api/broker/observation?journalId=${state.journal.journalId}&sequence=${state.journal.selectedSequence}&captureSha256=${report.sha256}`:"/api/broker/observation"} download="pramana-broker-observation.json">Export evidence</a><button onClick={()=>onAsk("Explain the broker order/trade observation status, its issue codes, and what remains unverified before pilot launch. Keep this external observation separate from our paper-account records.")}>Ask Atlas</button></div>
      <div className="research-table-scroll" role="region" tabIndex={0} aria-label="Broker execution evidence"><table><thead><tr><th scope="col">Order / instrument</th><th scope="col">Status</th><th scope="col">Side / product</th><th scope="col">Requested</th><th scope="col">Filled</th><th scope="col">Pending</th><th scope="col">Cancelled</th><th scope="col">Evidence</th></tr></thead><tbody>{rows.slice(currentPage*10,currentPage*10+10).map(o=><tr key={o.orderId}><td><strong>{o.symbol}</strong><br/><small>{o.exchange} · {o.orderId}</small></td><td>{o.status}</td><td>{o.side} · {o.product}</td><td>{o.quantity}</td><td>{o.filled}</td><td>{o.pending}</td><td>{o.cancelled}</td><td><button aria-label={`Inspect broker order ${o.orderId}`} onClick={()=>setSelected(o.orderId)}>Inspect</button></td></tr>)}</tbody></table></div>
      {!rows.length&&<p className="empty">No broker orders match this view.</p>}
      <div className="replay-pagination"><button disabled={!currentPage} onClick={()=>setPage(currentPage-1)}>Previous orders</button><span>Page {currentPage+1} of {maxPage+1} · {rows.length} orders</span><button disabled={currentPage===maxPage} onClick={()=>setPage(currentPage+1)}>Next orders</button></div>
      {chosen&&<div className="callout"><h3>{chosen.symbol} · {chosen.orderId}</h3><p>Instrument {chosen.instrumentId} · {chosen.variety} · broker status {chosen.status}. Order registered {new Date(chosen.at).toLocaleString()}.</p><p>{chosen.status==="COMPLETE"?`Broker completion average: ${chosen.averagePrice}. Compared with execution-weighted price within 0.01 in the instrument's quote units.`:"Completion-average comparison does not apply to this status."}</p><div className="research-table-scroll" role="region" tabIndex={0} aria-label="Broker execution evidence"><table><thead><tr><th scope="col">Execution</th><th scope="col">Quantity</th><th scope="col">Price</th><th scope="col">Fill time</th></tr></thead><tbody>{trades?.slice(0,100).map(t=><tr key={`${t.exchange}:${t.tradeId}`}><td>{t.tradeId}</td><td>{t.quantity}</td><td>{t.price}</td><td>{new Date(t.at).toLocaleString()}</td></tr>)}</tbody></table></div><p>{trades?.length||0} linked executions; at most 100 displayed. Full evidence is available in the export.</p></div>}
      <p className="footnote">Consistency is agreement between repeated broker reads, not an atomic snapshot or proof of successful local execution. Cash, holdings, fees and live/backtest parity are outside these checks. Retained history records observed states, not every broker event. Regular and AMO orders only; other varieties are flagged. No real orders can be placed here. Atlas receives counts and issue codes only.</p>
    </>}
  </section>;
}


export function BrokerObservation({state,onAsk}:{state?:BrokerObservationState;onAsk:(prompt:string,selection?:BrokerSelection)=>void}) {
  const [requested,setRequested]=useState<{journalId:string;sequence:number}|null>(null);
  const [historical,setHistorical]=useState<BrokerObservationState|null>(null),[error,setError]=useState("");
  const [sequence,setSequence]=useState("");
  const journal=state?.journal;
  useEffect(()=>{
    setHistorical(null);setError("");
    if(!requested)return;
    if(journal?.journalId!==requested.journalId){setError("The selected journal is no longer active. Return to the current capture.");return;}
    const controller=new AbortController();
    fetch(`/api/broker/observation?journalId=${requested.journalId}&sequence=${requested.sequence}`,{signal:controller.signal})
      .then(async r=>{const body=await r.json();if(!r.ok)throw new Error(body.detail||body.error||"Capture unavailable");return body as BrokerObservationState;})
      .then(body=>{if(!controller.signal.aborted)setHistorical(body);})
      .catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Capture unavailable");});
    return ()=>controller.abort();
  },[requested,journal?.journalId]);
  const active=requested?(historical?.journal?.journalId===requested.journalId&&historical.journal.selectedSequence===requested.sequence?historical:undefined):state;
  const view=active?.journal;
  const choose=(n:number)=>{if(journal&&Number.isSafeInteger(n)&&n>0&&n<=journal.captureCount){setHistorical(null);setRequested({journalId:journal.journalId,sequence:n});}};
  const selection:BrokerSelection|undefined=view&&active?.report?{journalId:view.journalId,sequence:view.selectedSequence,captureSha256:active.report.sha256}:undefined;
  return <>
    {(journal||requested)&&<section className="panel portfolio-replay" aria-label="Broker observation history">
      <div className="panel-title"><div><span className="eyebrow">RETAINED BROKER OBSERVATIONS</span><h2>Order lifecycle history</h2></div><span className="pill amber">{view?.temporal.status||"Unverified"}</span></div>
      <p className="muted">{journal?.captureCount??"Unknown"} retained captures. Review any sequence; the menu lists the latest 100. An observation gap can contain broker events that were never captured.</p>
      <div className="replay-selectors"><label>Recent capture<select aria-label="Broker history capture" value={requested?String(requested.sequence):"current"} onChange={e=>e.target.value==="current"?setRequested(null):choose(Number(e.target.value))}>
        <option value="current">Current capture</option>
        {requested&&!journal?.history.some(h=>h.sequence===requested.sequence)&&<option value={requested.sequence}>Selected capture #{requested.sequence}</option>}
        {[...(journal?.history||[])].reverse().map(h=><option key={h.sequence} value={h.sequence}>#{h.sequence} · {new Date(h.finishedAt).toLocaleString()} · {h.issueCount} issues</option>)}
      </select></label><label>Capture sequence<input aria-label="Broker capture sequence" type="number" min={1} max={journal?.captureCount} step={1} value={sequence} onChange={e=>setSequence(e.target.value)}/></label></div>
      <div className="research-actions"><button disabled={!journal||!/^([1-9]\d*)$/.test(sequence)||Number(sequence)>journal.captureCount} onClick={()=>choose(Number(sequence))}>Load capture</button><button disabled={!requested} onClick={()=>setRequested(null)}>Return to current capture</button></div>
      {error&&<p className="research-notice" role="alert">{error}</p>}
      {requested&&!historical&&!error&&<p role="status">Loading selected broker evidence…</p>}
      {view&&<>
        <p className="footnote">Selected capture #{view.selectedSequence} · preceding observation gap {view.temporal.gapSeconds===null?"unavailable":`${view.temporal.gapSeconds}s`} · {view.temporal.dayBoundary?"broker-day baseline":"same broker day"}.</p>
        <dl className="research-stats replay-metrics"><div><dt>Lifecycle issues at this capture</dt><dd>{view.temporal.issueCount}</dd></div><div><dt>Observed changes at this capture</dt><dd>{view.temporal.changeCount}</dd></div><div><dt>Retained issue occurrences through this capture</dt><dd>{view.cumulativeIssueCount}</dd></div><div><dt>Prior-day orders without observed terminal outcome</dt><dd>{view.temporal.unresolvedPriorDayOrders}</dd></div></dl>
        {view.temporal.status==="unverified"&&<p className="research-notice">The broker changed during this capture. It is retained for inspection but does not replace the prior stable comparison baseline.</p>}
        {!!view.cumulativeIssueCount&&<p className="research-notice">Earlier discrepancies remain in the journal even if current records agree. Counts are observations, not unique incidents or proof of financial loss.</p>}
        {!!view.temporal.issues.length&&<details open><summary>Lifecycle findings ({view.temporal.issues.length} of {view.temporal.issueCount})</summary><ul>{view.temporal.issues.map((issue,i)=><li key={i}>{issue.code.replaceAll("_"," ")}{issue.orderId?` · ${issue.orderId}`:""}</li>)}</ul></details>}
        <details><summary>Inspect observed changes ({view.temporal.changes.length} of {view.temporal.changeCount})</summary><div className="research-table-scroll" role="region" tabIndex={0} aria-label="Broker lifecycle changes"><table><thead><tr><th scope="col">Order</th><th scope="col">Observed field</th><th scope="col">Before</th><th scope="col">After</th></tr></thead><tbody>{view.temporal.changes.map((change,i)=><tr key={i}><td>{change.orderId}</td><td>{change.kind.replaceAll("_"," ")}</td><td>{change.before??"—"}</td><td>{change.after??"—"}</td></tr>)}</tbody></table></div></details>
        <p className="footnote">The full stored chain is checked within the configured replay bounds. Hashes establish internal continuity, not broker authenticity or absence of omitted captures. Retained states do not replace acknowledgements, postbacks, cash/position reconciliation or operational qualification.</p>
      </>}
    </section>}
    {(!requested||active)&&<BrokerObservationBody key={requested?`${requested.journalId}:${requested.sequence}`:"current"} state={active} onAsk={prompt=>onAsk(prompt,selection)}/>}
  </>;
}
