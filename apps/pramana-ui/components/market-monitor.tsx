"use client";
import { useEffect, useState } from "react";
type Row = { symbol: string; available: boolean; price?: number; change?: number | null; volume?: number; lastTrade?: string; exchangeTimestamp?: string; history?: {date: string; close: number}[] };
type Snapshot = { news?: {headline: string; publishedAt: string; source: string}[]; runtime?: {status: string; updatedAt?: string; halted?: boolean; cadenceFailures?: number; watchlist?: string[]}; status: string; fetchedAt?: string; session?: string; rows: Row[]; commodity?: string; note?: string; providers?: Record<string,string> };
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
      return <tr key={row.symbol} className="border-b border-slate-800"><td className="p-2 font-medium">{row.symbol}</td><td>{row.available ? row.price?.toLocaleString("en-IN") : "Unavailable"}</td><td>{row.change == null ? "—" : row.change.toFixed(2)+"%"}</td><td>{row.volume?.toLocaleString("en-IN") ?? "—"}</td><td>{values.length>1 ? <svg viewBox="0 0 140 32" width="140" height="32" role="img" aria-label={row.symbol+" historical daily closing prices"}><polyline fill="none" stroke="#22d3ee" strokeWidth="1.5" points={values.map((v,i)=>(i*140/(values.length-1))+","+(30-(v-min)/span*28)).join(" ")} /></svg> : "No history"}</td><td className="text-xs">{row.lastTrade || row.exchangeTimestamp || "Not supplied"}</td></tr>;
    })}</tbody></table></div>
    <p className="mt-4 text-sm text-amber-200">{data.commodity} · MCX contracts are separate from metal-company shares and ETFs.</p>
    <div className="mt-4 grid gap-2 md:grid-cols-3">{Object.entries(data.providers || {}).map(([name,status])=><div key={name} className="rounded border border-slate-800 p-3 text-xs"><strong>{name}</strong><p className="mt-1 text-slate-400">{status}</p></div>)}</div>
    <div className="mt-4 border-t border-slate-800 pt-3"><h3 className="text-sm font-semibold">Latest financial headlines</h3>{data.news?.length ? data.news.map((item,i)=><p key={i} className="mt-2 text-sm">{item.headline}<span className="block text-xs text-slate-500">{item.source} · {item.publishedAt}</span></p>) : <p className="text-xs text-slate-400">No dated headlines available.</p>}</div>
    <p className="mt-3 text-xs text-slate-500">Market watch is independent of the paper portfolio below. Empty holdings or proofs mean no persisted paper activity, not missing quotes.</p>
  </section>;
}
