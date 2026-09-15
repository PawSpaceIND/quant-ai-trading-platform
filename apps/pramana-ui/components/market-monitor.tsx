"use client";
import { useEffect, useState } from "react";
import type { IndiaCoverage } from "@/lib/india-universe";
type Row = { symbol: string; available: boolean; price?: number; change?: number | null; volume?: number; lastTrade?: string; exchangeTimestamp?: string; history?: {date: string; close: number}[]; instrument?: {market: string; assetClass: string; currency: string; exchange: string; contract?: string; expiry?: string} };
type Snapshot = { news?: {headline: string; publishedAt: string; source: string}[]; runtime?: {status: string; updatedAt?: string; halted?: boolean; cadenceFailures?: number; watchlist?: string[]}; status: string; fetchedAt?: string; session?: string; rows: Row[]; commodity?: string; note?: string; providers?: Record<string,string>; coverage?: IndiaCoverage };
export function MarketMonitor() {
  const [data, setData] = useState<Snapshot>({status:"loading", rows:[]});
  const [failed, setFailed] = useState(false);
  const [clock, setClock] = useState(0);
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try { const response = await fetch("/api/market", {cache:"no-store"}); if (!response.ok) throw new Error(); const next = await response.json(); if(active) {setData(next);setFailed(false);} }
      catch {if(active) setFailed(true);}
      if(active) setClock(Date.now());
    };
    void refresh(); const timer=setInterval(refresh,15000);
    return () => {active=false;clearInterval(timer);};
  }, []);
  const stale = failed || !data.fetchedAt || clock-Date.parse(data.fetchedAt)>120000;
  return <section className="rounded border border-slate-800 bg-slate-950/50 p-5 text-slate-300">
    <div className="flex flex-wrap justify-between gap-3"><h2 className="text-lg font-semibold text-cyan-300">India market watch</h2><span>{data.session || "SESSION UNKNOWN"} · {stale ? "COLLECTOR STALE / UNAVAILABLE" : "LAST AVAILABLE QUOTES"}</span></div>
    <p className="mt-3 text-sm text-cyan-200">Paper engine: {data.runtime?.status || "unknown"} · {data.runtime?.updatedAt && clock-Date.parse(data.runtime.updatedAt)>60000 ? "HEARTBEAT STALE" : data.runtime?.updatedAt || "No heartbeat"} · {data.runtime?.halted ? "HALTED" : "Paper only"} · Cadence failures: {data.runtime?.cadenceFailures ?? "—"}</p>
    <p className="my-2 text-sm text-slate-400">{data.note || "Waiting for the read-only quote collector."}</p>
    <p className="text-xs text-slate-400">Retrieved: {data.fetchedAt || "—"} · Data: Zerodha · Quotes refresh every 60 seconds · Read-only watchlist</p>
    <div className="mt-4 overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr className="border-b border-slate-700"><th className="p-2">Instrument</th><th>Last price</th><th>Change vs previous close</th><th>Volume</th><th>Recent daily closes</th><th>Last trade / exchange time</th></tr></thead><tbody>{data.rows.map(row => {
      const values=(row.history || []).map(x=>x.close); const min=Math.min(...values);const span=Math.max(...values)-min || 1;
      return <tr key={row.symbol} className="border-b border-slate-800"><td className="p-2 font-medium">{row.symbol}<span className="block text-xs text-slate-500">{row.instrument ? `${row.instrument.exchange} · ${row.instrument.currency} · ${row.instrument.assetClass}${row.instrument.contract ? ` · ${row.instrument.contract}${row.instrument.expiry ? ` · ${row.instrument.expiry}` : ""}` : ""}` : "Venue not supplied"}</span></td><td>{row.available ? row.price?.toLocaleString("en-IN") : "Unavailable"}</td><td>{row.change == null ? "—" : row.change.toFixed(2)+"%"}</td><td>{row.volume?.toLocaleString("en-IN") ?? "—"}</td><td>{values.length>1 ? <svg viewBox="0 0 140 32" width="140" height="32" role="img" aria-label={row.symbol+" historical daily closing prices"}><polyline fill="none" stroke="#22d3ee" strokeWidth="1.5" points={values.map((v,i)=>(i*140/(values.length-1))+","+(30-(v-min)/span*28)).join(" ")} /></svg> : "No history"}</td><td className="text-xs">{row.lastTrade || row.exchangeTimestamp || "Not supplied"}</td></tr>;
    })}</tbody></table></div>
    <p className="mt-4 text-sm text-amber-200">{data.commodity} · MCX contracts are separate from metal-company shares and ETFs.</p>
    {data.coverage?.groups?.length ? <section className="mt-5 rounded border border-slate-800 bg-slate-900/60 p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div><p className="text-[10px] uppercase tracking-[0.2em] text-cyan-300">INDIA COVERAGE</p><h3 className="mt-1 text-base font-semibold text-slate-200">{data.coverage.scope}</h3></div><span className="rounded border border-emerald-800 px-2 py-1 text-xs text-emerald-300">Paper only</span></div><div className="mt-3 grid gap-2 md:grid-cols-2">{data.coverage.groups.map(group => <article key={group.id} className="rounded border border-slate-800 p-3"><div className="flex items-start justify-between gap-2"><strong className="text-sm text-slate-200">{group.label}</strong><span className={`rounded px-2 py-0.5 text-[10px] ${group.status === "observed" ? "bg-emerald-950 text-emerald-300" : "bg-amber-950 text-amber-300"}`}>{group.status === "observed" ? "Observed" : group.mode === "requires_contract" ? "Contract needed" : "Planned"}</span></div><p className="mt-1 text-xs text-slate-500">{group.exchange} · {group.currency} · {group.assetClasses.join(" / ")}</p><p className="mt-2 text-xs text-slate-300">{group.examples.join(" · ")}</p><p className="mt-2 text-xs leading-5 text-slate-500">{group.detail}</p></article>)}</div><p className="mt-3 text-xs leading-5 text-slate-500">{data.coverage.disclaimer}</p><p className="mt-2 text-xs leading-5 text-slate-500"><strong>Atlas context:</strong> {data.coverage.aiContext}</p></section> : null}
    <div className="mt-4 grid gap-2 md:grid-cols-3">{Object.entries(data.providers || {}).map(([name,status])=><div key={name} className="rounded border border-slate-800 p-3 text-xs"><strong>{name}</strong><p className="mt-1 text-slate-400">{status}</p></div>)}</div>
    <div className="mt-4 border-t border-slate-800 pt-3"><h3 className="text-sm font-semibold">Latest financial headlines</h3>{data.news?.length ? data.news.map((item,i)=><p key={i} className="mt-2 text-sm">{item.headline}<span className="block text-xs text-slate-500">{item.source} · {item.publishedAt}</span></p>) : <p className="text-xs text-slate-400">No dated headlines available.</p>}</div>
    <p className="mt-3 text-xs text-slate-500">Market watch is independent of the paper portfolio below. Empty holdings or proofs mean no persisted paper activity, not missing quotes.</p>
  </section>;
}
