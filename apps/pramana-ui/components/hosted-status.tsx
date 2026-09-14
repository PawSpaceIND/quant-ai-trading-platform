"use client";
import {useEffect,useState} from "react";
export function HostedStatus(){
 const [data,setData]=useState<{sourceAt?:string;stale?:boolean}|null>(null);
 useEffect(()=>{let active=true;const poll=async()=>{try{const r=await fetch("/api/cloud-status",{cache:"no-store"});if(r.ok&&active)setData(await r.json());}catch{if(active)setData({stale:true});}};void poll();const t=setInterval(poll,15000);return()=>{active=false;clearInterval(t);};},[]);
 if(!data)return null;
 return <div className="border-b border-slate-700 bg-slate-950 p-3 text-center text-sm text-amber-200">Hosted paper dashboard · {data.stale ? "DATA SYNC STALE — CHECK ENGINE" : "Data synced"} · Last sync: {data.sourceAt ? new Date(data.sourceAt).toLocaleString("en-IN") : "Waiting"} · Engine currently runs on your Mac</div>;
}