"use client";

import type {Runtime} from "../lib/pilot";
const reasons:Record<string,string>={fresh:"Fresh tick",engine_unverified:"Engine heartbeat unverified",timestamp_unrecorded_or_invalid:"Timestamp unrecorded or invalid",future_tick:"Future timestamp",tick_expired:"Tick expired",engine_rejected_tick:"Engine did not accept this tick",duplicate_instrument:"Duplicate instrument",workspace_expired:"Workspace evidence expired"};

export function EngineFeedStatus({runtime,onAsk}:{runtime:Runtime;onAsk:(question:string)=>void}) {
  const rows=runtime.watchlist??[],fresh=rows.filter(r=>r.fresh).length;
  return <section className="panel" aria-label="Engine feed observations">
    <div className="panel-title"><div><span className="eyebrow">TRADING ENGINE SOURCES</span><h2>Engine feed observations</h2></div><span className={`pill ${fresh===rows.length&&rows.length>0?"green":"amber"}`}>{fresh}/{rows.length} verified ticks</span></div>
    <p className="footnote">Engine heartbeat {runtime.updatedAt?new Date(runtime.updatedAt).toLocaleString():"unrecorded"}. A tick needs a current engine heartbeat, an accepted source observation and a timestamp within 120 seconds. Collector refreshes do not refresh engine ticks.</p>
    {runtime.runtimeEvidenceIssue?<p className="research-notice">{runtime.runtimeEvidenceIssue}</p>:null}
    {rows.length?<div className="research-table-scroll" role="region" tabIndex={0} aria-label="Engine instrument timestamps" style={{maxHeight:360}}><table><thead><tr><th scope="col">Instrument</th><th scope="col">Status</th><th scope="col">Source timestamp</th><th scope="col">Source age</th></tr></thead><tbody>{rows.map((r,i)=><tr key={`${r.market}:${r.exchange}:${r.assetClass}:${r.symbol}:${i}`}><th scope="row">{r.symbol}<div className="muted">{r.exchange} · {r.assetClass}</div></th><td>{reasons[r.freshnessReason??""]??"Unverified"}</td><td>{r.tickTimestamp?<time dateTime={r.tickTimestamp} title={r.tickTimestamp}>{new Date(r.tickTimestamp).toLocaleString()}</time>:"Unrecorded"}</td><td>{typeof r.tickAgeSeconds==="number"?`${r.tickAgeSeconds.toLocaleString(undefined,{maximumFractionDigits:6})}s`:"Unknown"}</td></tr>)}</tbody></table></div>:<p className="empty">No validated engine instrument observations are available.</p>}
    <button onClick={()=>onAsk("Review current engine feed evidence using source timestamps, heartbeat freshness and per-instrument rejection reasons. Distinguish engine ticks from collector quotes and last observed portfolio marks. Explain missing, stale, future or duplicate observations without treating a stored fresh flag as current proof.")}>Discuss feed evidence with Atlas ↗</button>
  </section>;
}
