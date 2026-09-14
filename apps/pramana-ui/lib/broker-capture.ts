import {createHash} from "node:crypto";

export type BrokerOrder = {orderId:string; instrumentId:string; symbol:string; exchange:string; product:string; side:"BUY"|"SELL"; quantity:number; status:string; variety:string; filled:number; pending:number; cancelled:number; averagePrice:string; exchangeOrderId:string|null; at:string};
export type BrokerTrade = Pick<BrokerOrder,"orderId"|"instrumentId"|"symbol"|"exchange"|"product"|"side"|"quantity"|"at"> & {tradeId:string; exchangeOrderId:string; price:string};
export type BrokerCapture = {schema:"pramana.broker_observation.v1"; scope:"kite_daily_order_trade_consistency_only"; tenantId:string; broker:"zerodha-kite"; accountRef:string; startedAt:string; finishedAt:string; ordersBefore:BrokerOrder[]; tradesBefore:BrokerTrade[]; orders:BrokerOrder[]; trades:BrokerTrade[]; sha256:string};
export type BrokerInspection = {status:"consistent"|"issues"|"changing"|"empty"; issueCount:number; issues:{code:string;orderId:string|null}[]; orderCount:number; tradeCount:number; openOrderCount:number};
export type BrokerObservationState = {status:"available"|"stale"|"unavailable"|"invalid"; detail:string; report:BrokerCapture|null; inspection:BrokerInspection|null; journal?:import("./broker-lifecycle").JournalView};
function check(ok:unknown,message:string):asserts ok {if(!ok)throw new Error(message);}
const string=(v:unknown)=>typeof v==="string"&&v.length>0&&v.length<=160&&!/[\x00-\x1f]/.test(v);
const hash=(v:unknown)=>typeof v==="string"&&/^[a-f0-9]{64}$/.test(v);
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==="object"&&!Array.isArray(v);
const integer=(v:unknown)=>Number.isSafeInteger(v)&&(v as number)>=0;
const price=(v:unknown)=>typeof v==="string"&&/^\d{1,13}\.\d{8}$/.test(v)&&Number(v)<=1e12;
const timestamp=(v:unknown)=>typeof v==="string"&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?\+00:00$/.test(v)&&Number.isFinite(Date.parse(v))&&new Date(Date.parse(v)).toISOString().slice(0,19)===v.slice(0,19);
const day=(v:string)=>new Date(Date.parse(v)+330*60_000).toISOString().slice(0,10);
export function canonical(v:unknown):string {
  if(Array.isArray(v))return `[${v.map(canonical).join(",")}]`;
  if(object(v))return `{${Object.keys(v).sort().map(k=>`${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
  return JSON.stringify(v);
}
const common=["orderId","instrumentId","symbol","exchange","product","side","quantity","at"];
function rows(v:unknown,trades:boolean) {
  check(Array.isArray(v)&&v.length<=(trades?10000:5000),"Broker collection exceeds bounds");
  const seen=new Set<string>();
  const keys=[...common,...(trades?["tradeId","exchangeOrderId","price"]:["status","variety","filled","pending","cancelled","averagePrice","exchangeOrderId"])].sort();
  for(const row of v) {
    check(object(row)&&Object.keys(row).sort().join(",")===keys.join(","),"Unexpected broker row fields");
    check(common.filter(k=>!["quantity","at"].includes(k)).every(k=>string(row[k]))&&["BUY","SELL"].includes(row.side as string)&&integer(row.quantity)&&Number(row.quantity)>0&&timestamp(row.at),"Invalid broker identity, quantity or time");
    check(typeof row.instrumentId==="string"&&/^[1-9]\d*$/.test(row.instrumentId)&&Number.isSafeInteger(Number(row.instrumentId)),"Invalid instrument ID");
    let key:string;
    if(trades) {
      check(string(row.tradeId)&&string(row.exchangeOrderId)&&price(row.price)&&Number(row.price)>0,"Invalid broker trade");
      key=JSON.stringify([row.exchange,row.tradeId]);
    } else {
      check(string(row.status)&&string(row.variety)&&["filled","pending","cancelled"].every(k=>integer(row[k]))&&price(row.averagePrice)&&(row.exchangeOrderId===null||string(row.exchangeOrderId)),"Invalid broker order");
      key=row.orderId as string;
    }
    check(!seen.has(key),"Duplicate broker identity");seen.add(key);
  }
}
const terminal=new Set(["COMPLETE","CANCELLED","REJECTED"]);
const statuses=new Set([...terminal,"OPEN","TRIGGER PENDING","VALIDATION PENDING","PUT ORDER REQ RECEIVED","OPEN PENDING","MODIFY VALIDATION PENDING","MODIFY PENDING","CANCEL PENDING","AMO REQ RECEIVED"]);
const money=(v:string)=>BigInt(v.replace(".",""));
export function inspectBrokerCapture(c:BrokerCapture):BrokerInspection {
  const issues:BrokerInspection["issues"]=[];let issueCount=0;
  const issue=(code:string,orderId:string|null=null)=>{issueCount++;if(issues.length<50)issues.push({code,orderId});};
  const orders=new Map(c.orders.map(o=>[o.orderId,o]));
  const fills=new Map<string,BrokerTrade[]>();
  for(const t of c.trades) {
    const o=orders.get(t.orderId);
    if(!o)issue("orphan_trade",t.orderId);
    else if((["instrumentId","symbol","exchange","product","side","exchangeOrderId"] as const).some(k=>t[k]!==o[k]))issue("trade_identity_mismatch",t.orderId);
    if(day(t.at)!==day(c.finishedAt)||Date.parse(t.at)>Date.parse(c.finishedAt))issue("trade_time_outside_capture_day",t.orderId);
    if(o&&Date.parse(t.at)<Date.parse(o.at))issue("trade_precedes_order",t.orderId);
    const linked=fills.get(t.orderId)||[];linked.push(t);fills.set(t.orderId,linked);
  }
  for(const o of c.orders) {
    const linked=fills.get(o.orderId)||[]; const qty=linked.reduce((s,t)=>s+BigInt(t.quantity),BigInt(0));
    if(Date.parse(o.at)>Date.parse(c.finishedAt))issue("order_time_after_capture",o.orderId);
    if(!statuses.has(o.status))issue("unsupported_status",o.orderId);
    if(!["regular","amo"].includes(o.variety))issue("unsupported_order_variety",o.orderId);
    if(BigInt(o.filled)!==qty)issue("filled_quantity_mismatch",o.orderId);
    if(BigInt(o.filled)+BigInt(o.cancelled)>BigInt(o.quantity)||o.pending>o.quantity)issue("quantity_components_exceed_order",o.orderId);
    if(o.status==="COMPLETE") {
      if(o.filled!==o.quantity||o.pending||o.cancelled)issue("complete_order_quantities",o.orderId);
      if(qty) {
        const deviation=linked.reduce((s,t)=>s+money(t.price)*BigInt(t.quantity),BigInt(0))-money(o.averagePrice)*qty;
        if((deviation<BigInt(0)?-deviation:deviation)>BigInt(1000000)*qty)issue("complete_average_price_mismatch",o.orderId);
      }
    }
    if(o.status==="REJECTED"&&o.filled)issue("rejected_order_has_fills",o.orderId);
  }
  const stable=canonical(c.ordersBefore)===canonical(c.orders)&&canonical(c.tradesBefore)===canonical(c.trades);
  return {status:!stable?"changing":issueCount?"issues":orders.size?"consistent":"empty",issueCount,issues,orderCount:c.orders.length,tradeCount:c.trades.length,openOrderCount:c.orders.filter(o=>!terminal.has(o.status)).length};
}
export function parseBrokerCapture(raw:string,tenant:string,accountRef:string):BrokerCapture {
  const c:unknown=JSON.parse(raw);
  check(object(c),"Invalid broker capture");
  check(Object.keys(c).sort().join(",")===["schema","scope","tenantId","broker","accountRef","startedAt","finishedAt","ordersBefore","tradesBefore","orders","trades","sha256"].sort().join(","),"Unexpected capture fields");
  check(c.schema==="pramana.broker_observation.v1"&&c.scope==="kite_daily_order_trade_consistency_only"&&c.broker==="zerodha-kite"&&c.tenantId===tenant&&hash(c.accountRef)&&c.accountRef===accountRef&&hash(c.sha256),"Broker account or schema mismatch");
  check(timestamp(c.startedAt)&&timestamp(c.finishedAt),"Invalid broker capture interval");
  const start=c.startedAt as string,end=c.finishedAt as string,duration=Date.parse(end)-Date.parse(start);
  check(duration>=0&&duration<=30_000&&day(start)===day(end),"Broker capture interval exceeds scope");
  rows(c.ordersBefore,false);rows(c.orders,false);rows(c.tradesBefore,true);rows(c.trades,true);
  const {sha256,...unsigned}=c;
  check(createHash("sha256").update(canonical(unsigned)).digest("hex")===sha256,"Broker evidence hash mismatch");
  return c as unknown as BrokerCapture;
}
