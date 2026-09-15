import fs from "node:fs";
import {tenantId} from "./db";
import {parseBrokerCapture,inspectBrokerCapture,type BrokerObservationState} from "./broker-capture";
import {readBrokerJournal} from "./broker-journal";
import type {BrokerSelection} from "./broker-lifecycle";
export {parseBrokerCapture,inspectBrokerCapture} from "./broker-capture";
export type {BrokerCapture,BrokerOrder,BrokerTrade,BrokerInspection,BrokerObservationState} from "./broker-capture";
function check(ok:unknown,message:string):asserts ok {if(!ok)throw new Error(message);}
export function readBrokerObservation(now=Date.now(),selection?:Omit<BrokerSelection,"captureSha256"> & {captureSha256?:string}):BrokerObservationState {
  if(process.env.PRAMANA_BROKER_JOURNAL) {
    if(process.env.PRAMANA_BROKER_OBSERVATION)return {status:"invalid",detail:"Select one broker evidence source: journal or single capture.",report:null,inspection:null};
    return readBrokerJournal(now,selection);
  }
  if(selection)return {status:"invalid",detail:"A selected historical capture requires its original journal.",report:null,inspection:null};
  const file=process.env.PRAMANA_BROKER_OBSERVATION,ref=process.env.PRAMANA_BROKER_ACCOUNT_REF;
  if(!file||!ref)return {status:"unavailable",detail:"No selected broker observation. Capture and bind an external account report to inspect order/trade consistency.",report:null,inspection:null};
  try {
    const fd=fs.openSync(/* turbopackIgnore: true */ file,"r");let raw;
    try {check(fs.fstatSync(fd).isFile()&&fs.fstatSync(fd).size<=8_000_000,"Broker capture file exceeds bounds");const buffer=Buffer.alloc(8_000_001);let size=0,got=0;do {got=fs.readSync(fd,buffer,size,buffer.length-size,null);size+=got;}while(got&&size<buffer.length);check(size<=8_000_000,"Broker capture grew past bounds");raw=buffer.subarray(0,size).toString("utf8");}finally{fs.closeSync(fd);}
    const report=parseBrokerCapture(raw,tenantId,ref),age=now-Date.parse(report.finishedAt);
    check(age>=-5000,"Broker capture is from the future");
    return {status:age>120000?"stale":"available",detail:age>120000?"Historical observation: older than 120 seconds. Current broker state is unverified.":"Repeated broker order/trade reads only. Separate from the paper account; no reconciliation to positions, cash, local live orders or strategy backtests.",report,inspection:inspectBrokerCapture(report)};
  } catch {return {status:"invalid",detail:"Broker evidence is invalid or does not match the selected account. No consistency result is available.",report:null,inspection:null};}
}
export function brokerObservationContext(selection?:BrokerSelection) {
  const {status,detail,report,inspection,journal}=readBrokerObservation(Date.now(),selection);
  if(selection&&!report)throw new Error("Selected broker evidence is unavailable or has changed");
  return {status,detail,scope:report?.scope,asOf:report?.finishedAt,
    lifecycle:journal?{sequence:journal.selectedSequence,asOf:report?.finishedAt,cumulativeIssueCount:journal.cumulativeIssueCount,
      temporal:{...journal.temporal,changes:undefined,issues:journal.temporal.issues.map(({code})=>({code}))}}:null,inspection:inspection?{...inspection,issues:inspection.issues.map(({code})=>({code}))}:null,
    omissions:"External account identity, instrument names, order IDs, trade details, prices and quantities are excluded. This observation cannot establish paper-account reconciliation, live/backtest parity or launch readiness."};
}
