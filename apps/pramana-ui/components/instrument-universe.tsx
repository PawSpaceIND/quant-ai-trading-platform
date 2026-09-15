"use client";
import type { InstrumentUniverse } from "@/lib/market";
import { STRATEGY_CATALOG } from "@/lib/strategy-catalog";

const top = (values: Record<string, number> | undefined, limit = 8) =>
  Object.entries(values || {}).sort((a,b)=>b[1]-a[1]).slice(0, limit);

export function InstrumentUniversePanel({universe}: {universe?: InstrumentUniverse}) {
  if (!universe) return null;
  const available = universe.status === "available";
  const breakdowns: Array<[string, Record<string, number>]> = [
    ["Venues", universe.byExchange],
    ["Segments", universe.bySegment],
    ["Asset classes", universe.byAssetClass],
  ];
  return <div className="market-coverage callout">
    <div className="section-row">
      <div><span className="eyebrow">BROKER INSTRUMENT MASTER</span><h3>Complete available universe</h3></div>
      <span className={`pill ${available ? "green" : "amber"}`}>{available ? `${universe.total.toLocaleString("en-IN")} exact records` : "Unavailable"}</span>
    </div>
    <p className="footnote">{available ? `Source: ${universe.source} · refreshed ${universe.fetchedAt || "—"}` : (universe.entitlement || "Configure the broker instrument master.")}</p>
    {available ? <><div className="coverage-grid">
      {breakdowns.map(([label, values]) => <article key={String(label)} className="coverage-card"><strong>{label}</strong>{top(values as Record<string,number>).map(([name,count])=><p key={name} className="coverage-examples">{name} <span className="muted">· {count.toLocaleString("en-IN")}</span></p>)}</article>)}
    </div>
    <div className="details mt-3"><div><dt>Options</dt><dd>{universe.optionContracts.toLocaleString("en-IN")}</dd></div><div><dt>Futures / FX / commodities</dt><dd>{universe.futureContracts.toLocaleString("en-IN")}</dd></div><div><dt>Expiring contract dates</dt><dd>{universe.expiringContracts.toLocaleString("en-IN")}</dd></div></div>
    {universe.sample?.length ? <p className="footnote">Sample exact broker contracts: {universe.sample.slice(0,8).map((item)=><span key={item.exchange+":"+item.contract} className="mr-2 inline-block">{item.exchange}:{item.contract}{item.expiry ? ` (${item.expiry})` : ""}</span>)}</p> : null}</> : null}
    <p className="footnote">{universe.shortSide}</p><p className="footnote">This master discovers exact instruments; it does not approve live orders. Quotes are polled only for the bounded read-only watchlist.</p>
    {available ? <a className="text-cyan-300 underline text-sm" href="/api/market/universe?limit=500" target="_blank" rel="noreferrer">Browse exact contracts and option legs ↗</a> : null}
    <div className="mt-4"><span className="eyebrow">STRATEGY RESEARCH CATALOG</span><div className="coverage-grid mt-2">{STRATEGY_CATALOG.map(strategy=><article key={strategy.id} className="coverage-card"><div className="coverage-card-title"><strong>{strategy.label}</strong><span className={`pill ${strategy.status==="paper_only" ? "green" : "amber"}`}>{strategy.status.replace("_"," ")}</span></div><small>{strategy.products.join(" · ")} · {strategy.sides.join(" / ")}</small><p className="footnote">{strategy.requiredEvidence.join(" · ")}</p></article>)}</div></div>
  </div>;
}
