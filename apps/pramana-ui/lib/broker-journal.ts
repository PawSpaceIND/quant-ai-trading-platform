import fs from "node:fs";
import {createHash} from "node:crypto";
import {DatabaseSync} from "node:sqlite";
import {tenantId} from "./db";
import {canonical,parseBrokerCapture,inspectBrokerCapture,type BrokerObservationState} from "./broker-capture";
import {BrokerLifecycle,type BrokerSelection,type JournalHistory,type JournalView} from "./broker-lifecycle";
function check(ok:unknown,message:string):asserts ok {if(!ok)throw new Error(message);}
const hash=(value:unknown)=>createHash("sha256").update(canonical(value)).digest("hex");
export function readBrokerJournal(now=Date.now(),selection?:Omit<BrokerSelection,"captureSha256"> & {captureSha256?:string}):BrokerObservationState {
  const file=process.env.PRAMANA_BROKER_JOURNAL,ref=process.env.PRAMANA_BROKER_ACCOUNT_REF;
  if(!file||!ref)return {status:"unavailable",detail:"No broker journal and matching account reference are configured.",report:null,inspection:null};
  let db:DatabaseSync|undefined;
  try {
    check(!fs.lstatSync(/* turbopackIgnore: true */ file).isSymbolicLink(),"Broker journal symlink is unsupported");
    db=new DatabaseSync(file,{readOnly:true});db.exec("PRAGMA query_only=ON; BEGIN");
    const tables=db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as {name:string}[];
    check(tables.map(r=>r.name).join(",")==="broker_captures,broker_journal_meta","Broker journal schema mismatch");
    const meta=db.prepare("SELECT id,version,journal_id,tenant,account_ref FROM broker_journal_meta").all() as {id:number;version:number;journal_id:string;tenant:string;account_ref:string}[];
    check(meta.length===1&&meta[0].id===1&&meta[0].version===1&&meta[0].tenant===tenantId&&meta[0].account_ref===ref&&/^[a-f0-9]{64}$/.test(ref)&&/^[a-f0-9]{32}$/.test(meta[0].journal_id),"Broker journal account mismatch");
    if(selection)check(Number.isSafeInteger(selection.sequence)&&selection.sequence>0&&selection.journalId===meta[0].journal_id&&(selection.captureSha256===undefined||/^[a-f0-9]{64}$/.test(selection.captureSha256)),"Broker journal selection changed");
    const counts=db.prepare("SELECT count(*) AS count,coalesce(sum(length(cast(payload AS BLOB))),0) AS bytes FROM broker_captures").get() as {count:number;bytes:number};
    check(counts.count<=10000&&counts.bytes<=128_000_000,"Broker journal exceeds verified replay bounds");
    if(!counts.count) {check(!selection,"No selected capture");return {status:"unavailable",detail:"The selected broker journal contains no captures.",report:null,inspection:null};}
    const selectedSequence=selection?.sequence??counts.count;
    check(selectedSequence<=counts.count,"Selected broker capture is missing");
    let previousHash:string|null=null,lastEnd:number|null=null,sequence=0,selected:BrokerObservationState|null=null;
    const lifecycle=new BrokerLifecycle(),history:JournalHistory[]=[];
    for(const row of db.prepare("SELECT sequence,previous_hash,entry_hash,capture_sha,payload FROM broker_captures ORDER BY sequence").iterate()) {
      sequence++;
      check(row.sequence===sequence&&row.previous_hash===previousHash&&row.entry_hash===hash({journalId:meta[0].journal_id,sequence,previousHash,captureSha256:row.capture_sha}),"Broker journal chain changed");
      check(typeof row.payload==="string"&&Buffer.byteLength(row.payload)<=8_000_000,"Invalid capture body");
      const capture=parseBrokerCapture(row.payload,tenantId,ref);
      check(row.capture_sha===capture.sha256&&(lastEnd===null||Date.parse(capture.startedAt)>=lastEnd),"Broker capture hash or chronology mismatch");
      check(Date.parse(capture.finishedAt)<=now+5000,"Future evidence in broker journal");
      previousHash=row.entry_hash as string;lastEnd=Date.parse(capture.finishedAt);
      const inspection=inspectBrokerCapture(capture),temporal=lifecycle.inspect(capture,inspection);
      history.push({sequence,day:new Date(Date.parse(capture.finishedAt)+330*60000).toISOString().slice(0,10),finishedAt:capture.finishedAt,captureSha256:capture.sha256,inspectionStatus:inspection.status,temporalStatus:temporal.status,issueCount:inspection.issueCount+temporal.issueCount,cumulativeIssueCount:lifecycle.cumulativeIssueCount});
      if(history.length>100)history.shift();
      if(sequence===selectedSequence) {
        if(selection?.captureSha256)check(selection.captureSha256===capture.sha256,"Selected broker evidence changed");
        const age=now-Date.parse(capture.finishedAt);check(age>=-5000,"Broker capture is from the future");
        selected={status:age>120000?"stale":"available",detail:age>120000?"Historical broker observation. Current state is unverified; retained lifecycle findings remain visible.":"Stored broker observations with independently replayed history. Observed transitions can miss intermediate events; this is separate from paper execution.",
          report:capture,inspection,journal:{journalId:meta[0].journal_id,headHash:"",captureCount:counts.count,history:[],historyOmitted:Math.max(0,counts.count-100),selectedSequence,selectedEntryHash:previousHash,temporal,cumulativeIssueCount:lifecycle.cumulativeIssueCount}};
      }
    }
    check(selected&&sequence===counts.count&&previousHash,"Incomplete broker journal read");
    (selected.journal as JournalView).headHash=previousHash;(selected.journal as JournalView).history=history;
    return selected;
  } catch {return {status:"invalid",detail:"Broker journal evidence is invalid, incomplete or does not match the selected account/capture. No lifecycle result is available.",report:null,inspection:null};}
  finally {db?.close();}
}
