import type {BrokerCapture} from "./broker-capture";
export type BrokerSelection={journalId:string;sequence:number;captureSha256:string};
export type TemporalReport={status:"unverified"|"issues"|"compared"|"baseline";issueCount:number;issues:{code:string;orderId:string|null}[];changeCount:number;changes:{kind:string;orderId:string;before:string|number|null;after:string|number|null}[];dayBoundary:boolean;unresolvedPriorDayOrders:number;gapSeconds:number|null};
export type JournalHistory={sequence:number;day:string;finishedAt:string;captureSha256:string;inspectionStatus:string;temporalStatus:string;issueCount:number;cumulativeIssueCount:number};
export type JournalView={journalId:string;headHash:string;captureCount:number;history:JournalHistory[];historyOmitted:number;selectedSequence:number;selectedEntryHash:string;temporal:TemporalReport;cumulativeIssueCount:number};
const terminal=new Set(["COMPLETE","CANCELLED","REJECTED"]);
const day=(at:string)=>new Date(Date.parse(at)+330*60000).toISOString().slice(0,10);

export class BrokerLifecycle {
  private orders=new Map<string,BrokerCapture["orders"][number]>();
  private trades=new Map<string,BrokerCapture["trades"][number]>();
  private terminals=new Map<string,BrokerCapture["orders"][number]>();
  private exchangeIds=new Map<string,string>();
  private maxFilled=new Map<string,number>();
  private previous:BrokerCapture|null=null;
  private currentDay:string|null=null;
  private stableSeen=false;
  cumulativeIssueCount=0;
  inspect(c:BrokerCapture,inspection:{status:string;issueCount:number}):TemporalReport {
    const issues:TemporalReport["issues"]=[],changes:TemporalReport["changes"]=[];
    let issueCount=0,changeCount=0;
    const issue=(code:string,orderId:string|null=null)=>{issueCount++;if(issues.length<100)issues.push({code,orderId});};
    const change=(kind:string,orderId:string,before:string|number|null=null,after:string|number|null=null)=>{changeCount++;if(changes.length<250)changes.push({kind,orderId,before,after});};
    const boundary=day(c.finishedAt)!==this.currentDay;
    let unresolved=0;
    if(boundary) {
      unresolved=[...this.orders.values()].filter(o=>!terminal.has(o.status)).length;
      if(this.currentDay&&unresolved)issue("day_boundary_unresolved_orders");
      this.orders.clear();this.trades.clear();this.terminals.clear();this.maxFilled.clear();this.exchangeIds.clear();this.currentDay=day(c.finishedAt);this.stableSeen=false;
    }
    const comparable=this.stableSeen&&inspection.status!=="changing";
    if(inspection.status!=="changing") {
      const orders=new Map(c.orders.map(o=>[o.orderId,o])), trades=new Map(c.trades.map(t=>[JSON.stringify([t.exchange,t.tradeId]),t]));
      for(const [key] of this.orders)if(!orders.has(key))issue("previously_observed_order_missing",key);
      for(const [key,old] of this.trades)if(!trades.has(key))issue("previously_observed_trade_missing",old.orderId);
      for(const [key,o] of orders) {
        const old=this.orders.get(key);
        if(old) {
          if((["instrumentId","symbol","exchange","product","side","variety"] as const).some(f=>old[f]!==o[f]))issue("order_identity_changed",key);
          if(this.exchangeIds.has(key)&&this.exchangeIds.get(key)!==o.exchangeOrderId)issue("exchange_order_identity_changed",key);
          if(o.filled<this.maxFilled.get(key)!)issue("filled_quantity_regressed",key);
          const ended=this.terminals.get(key);
          if(ended&&((ended.status==="COMPLETE"?["status","quantity","filled","averagePrice"]:["status","quantity","filled"]) as Array<keyof typeof o>).some(f=>o[f]!==ended[f]))issue("terminal_order_changed",key);
          for(const field of ["status","quantity","filled","pending","cancelled","averagePrice","exchangeOrderId","at"] as const)
            if(old[field]!==o[field])change(field,key,old[field],o[field]);
        } else change("order_first_observed",key,null,o.status);
        if(o.exchangeOrderId!==null&&!this.exchangeIds.has(key))this.exchangeIds.set(key,o.exchangeOrderId);
        this.maxFilled.set(key,Math.max(this.maxFilled.get(key)||0,o.filled));
        if(terminal.has(o.status)&&!this.terminals.has(key))this.terminals.set(key,{...o});
        this.orders.set(key,old?{...old,status:o.status,quantity:o.quantity,filled:o.filled,pending:o.pending,cancelled:o.cancelled,averagePrice:o.averagePrice,exchangeOrderId:o.exchangeOrderId,at:o.at}:{...o});
      }
      for(const [key,t] of trades) {
        const old=this.trades.get(key);
        if(old) {
          if((Object.keys(t) as Array<keyof typeof t>).some(f=>old[f]!==t[f]))issue("previous_execution_changed",t.orderId);
        } else {this.trades.set(key,{...t});change("execution_first_observed",t.orderId,null,t.tradeId);}
      }
      this.stableSeen=true;
    }
    this.cumulativeIssueCount+=issueCount+inspection.issueCount;
    const result:TemporalReport={status:inspection.status==="changing"?"unverified":issueCount?"issues":comparable?"compared":"baseline",issueCount,issues,changeCount,changes,dayBoundary:boundary,unresolvedPriorDayOrders:unresolved,gapSeconds:this.previous?(Date.parse(c.startedAt)-Date.parse(this.previous.finishedAt))/1000:null};
    this.previous=c;return result;
  }
}
export function validBrokerSelection(v:unknown):v is BrokerSelection {
  if(!v||typeof v!=="object"||Array.isArray(v))return false;
  const s=v as Record<string,unknown>;
  return Object.keys(s).sort().join(",")==="captureSha256,journalId,sequence"&&typeof s.journalId==="string"&&/^[a-f0-9]{32}$/.test(s.journalId)&&Number.isSafeInteger(s.sequence)&&Number(s.sequence)>0&&typeof s.captureSha256==="string"&&/^[a-f0-9]{64}$/.test(s.captureSha256);
}
