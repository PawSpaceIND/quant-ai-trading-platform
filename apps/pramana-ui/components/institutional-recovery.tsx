"use client";
import {useEffect,useRef,useState} from "react";
import {RECOVERY_CONFIRMATION,recoveryId,type RecoveryOutcome,type RecoveryPreview} from "@/lib/institutional-recovery-contract";
const storedAttempt="pramana.bookkeeping-recovery.attempt.v1";
type Attempt={tenant_id:string;program_id:string;request_id:string;expected_context_sha256:string};
export function InstitutionalRecoveryPanel() {
  const [program,setProgram]=useState(""),[preview,setPreview]=useState<RecoveryPreview|null>(null);
  const [confirmed,setConfirmed]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState("");
  const [attempt,setAttempt]=useState<Attempt|null>(null),[outcome,setOutcome]=useState<RecoveryOutcome|null>(null);
  const [trackingBlocked,setTrackingBlocked]=useState(false);
  const [lookup,setLookup]=useState("");const inProgress=useRef(false),mounted=useRef(true);
  useEffect(()=>{mounted.current=true;try {
    const raw=sessionStorage.getItem(storedAttempt);
    if(raw){const saved=JSON.parse(raw) as Attempt;
      if(recoveryId(saved.tenant_id)&&recoveryId(saved.program_id)&&recoveryId(saved.request_id)&&/^[a-f0-9]{64}$/.test(saved.expected_context_sha256)){
        setAttempt(saved);setProgram(saved.program_id);setLookup(saved.request_id);
        setError("A previous request has no fresh confirmation here. Check its saved outcome; do not submit it again.");
      }else{throw new Error("Invalid saved request");}}
  }catch{setTrackingBlocked(true);setError("Local request tracking is unavailable. Recovery cannot be submitted until tracking works.");}
    return()=>{mounted.current=false;};},[]);
  async function perform<T,>(action:()=>Promise<T>,apply:(value:T)=>void) {
    if(inProgress.current)return;inProgress.current=true;setBusy(true);setError("");
    try{const value=await action();if(mounted.current)apply(value);}
    catch{if(mounted.current)setError("Observation unavailable. No clean-account or no-change claim is made.");}
    finally{inProgress.current=false;if(mounted.current)setBusy(false);}
  }
  async function inspect(){
    setPreview(null);setConfirmed(false);setOutcome(null);
    await perform(async()=>{const r=await fetch(`/api/institutional-recovery?program_id=${encodeURIComponent(program)}`,{cache:"no-store"});
      if(!r.ok)throw new Error();return await r.json() as RecoveryPreview;},value=>setPreview(value));
  }
  async function reconcile(){
    if(inProgress.current||!preview?.canApply||!confirmed||attempt||trackingBlocked)return;
    const next={tenant_id:preview.tenant_id,program_id:preview.program_id,request_id:`recovery-${crypto.randomUUID()}`,expected_context_sha256:preview.context_sha256};
    // Persist only non-secret correlation IDs before dispatch. Never auto-replay a POST.
    try{sessionStorage.setItem(storedAttempt,JSON.stringify(next));}
    catch{setError("Request tracking could not be saved. Nothing was submitted.");return;}
    setAttempt(next);setLookup(next.request_id);setOutcome(null);setConfirmed(false);
    await perform(async()=>{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
      try {const r=await fetch("/api/institutional-recovery",{method:"POST",cache:"no-store",signal:controller.signal,
          headers:{"Content-Type":"application/json"},body:JSON.stringify({program_id:next.program_id,request_id:next.request_id,expected_context_sha256:next.expected_context_sha256,confirmation:RECOVERY_CONFIRMATION})});
        if(!r.ok)throw new Error();return await r.json() as RecoveryOutcome;
      }finally{clearTimeout(timer);}},value=>setOutcome(value));
  }
  async function checkOutcome(){
    setOutcome(null);
    await perform(async()=>{const r=await fetch(`/api/institutional-recovery?request_id=${encodeURIComponent(lookup)}`,{cache:"no-store"});
      if(!r.ok)throw new Error();const value=await r.json() as RecoveryOutcome;
      if(attempt?.request_id===lookup&&(value.tenant_id!==attempt.tenant_id||value.program_id!==attempt.program_id||value.context_sha256!==attempt.expected_context_sha256))throw new Error("Recovery selection changed");
      return value;},value=>setOutcome(value));
  }
  function reset(){
    if(!outcome||outcome.status!=="RETURNED"||outcome.request_id!==attempt?.request_id)return;
    try{sessionStorage.removeItem(storedAttempt);}catch{return;}
    setAttempt(null);setPreview(null);setOutcome(null);setConfirmed(false);setError("");setProgram("");setLookup("");
  }
  return <section className="panel" aria-label="Institutional bookkeeping recovery">
    <span className="eyebrow">EXPLICIT OPERATOR REVIEW · PAPER BOOKKEEPING ONLY</span>
    <h2>Review recorded bookkeeping</h2>
    <p className="muted">This workflow reconciles retained paper fills and recorded protective-exit bookkeeping. It cannot place a trade, clear a halt, reopen a position or release reserved risk. Server configuration and separate API permissions are required.</p>
    <label style={{display:"block"}}>Execution programme ID
      <input aria-label="Execution programme ID" value={program} disabled={busy||!!attempt} maxLength={180}
        style={{display:"block",width:"100%",maxWidth:"36rem"}} onChange={e=>{setProgram(e.target.value);setPreview(null);setConfirmed(false);}} />
    </label>
    <button className="btn" type="button" disabled={busy||!!attempt||!recoveryId(program)} onClick={()=>void inspect()}>Inspect saved programme</button>
    {preview&&<div className="callout" style={{overflowWrap:"anywhere"}}>
      <p><strong>Recovery account: {preview.tenant_id}</strong> · Recorded state: {preview.program_state}</p>
      <p>Slice state: {preview.slice_states.join(", ")}</p>
      <p className="footnote">Exact saved context: <code>{preview.context_sha256}</code></p>
      {!preview.canApply&&<p>Read-only gateway. Applying recovery is not configured.</p>}
      <label style={{display:"flex",gap:"0.6rem",alignItems:"flex-start"}}><input type="checkbox" checked={confirmed}
        disabled={busy||!!attempt||trackingBlocked||!preview.canApply} onChange={e=>setConfirmed(e.target.checked)} />
        <span>I have reviewed this programme and account. Reconcile recorded paper bookkeeping only; do not place orders or clear halts.</span></label>
      <button className="btn" type="button" disabled={busy||!!attempt||trackingBlocked||!confirmed||!preview.canApply} onClick={()=>void reconcile()}>Confirm bookkeeping reconciliation</button>
    </div>}
    {attempt&&<div className="callout" style={{overflowWrap:"anywhere"}}>
      <strong>{outcome?.request_id===attempt.request_id?"Recorded request result":"Outcome not confirmed"}</strong>
      <p>Selected recovery account: {attempt.tenant_id}</p>
      <p>Request reference: <code data-testid="recovery-request-id">{attempt.request_id}</code></p>
      <p>Do not repeat submission after an interruption. Check this saved request. Missing results do not prove that nothing changed.</p>
    </div>}
    <div aria-live="polite">{busy&&<p>Checking recorded state…</p>}{error&&<p role="alert" className="callout">{error}</p>}
      {outcome&&<div className="callout" data-testid="recovery-outcome" style={{overflowWrap:"anywhere"}}>
        <p><strong>Audit status: {outcome.status}</strong> · Account: {outcome.tenant_id}</p>
        <p>Request: <code>{outcome.request_id}</code> · Programme: <code>{outcome.program_id}</code></p>
        <p>Recorded at {outcome.recorded_at}. Historical outcome, not current account health or trading permission.</p>
        {outcome.status==="RETURNED"?<><p>Reconciliation stage: {outcome.result?.recovery_stage}</p>
          <p>Original broker references: {outcome.result?.committed_order_ids?.join(", ")||"None recorded"}</p></>
          :<p>Operator review remains required. No automatic retry is permitted.</p>}
      </div>}
    </div>
    <label style={{display:"block"}}>Saved recovery request ID
      <input aria-label="Saved recovery request ID" value={lookup} disabled={busy} maxLength={180}
        style={{display:"block",width:"100%",maxWidth:"36rem"}} onChange={e=>{setLookup(e.target.value);setOutcome(null);}} />
    </label>
    <button className="btn" type="button" disabled={busy||!recoveryId(lookup)} onClick={()=>void checkOutcome()}>Check saved outcome</button>
    {attempt&&outcome?.request_id===attempt.request_id&&outcome.status==="RETURNED"&&
      <button className="btn" type="button" disabled={busy} onClick={reset}>Review another programme</button>}
  </section>;
}
