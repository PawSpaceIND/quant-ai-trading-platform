import {test} from "node:test";
import assert from "node:assert/strict";
import {INDIA_COVERAGE_GROUPS,indiaCoverage} from "../lib/india-universe";
import type {MarketRow} from "../lib/market";

const row=(symbol:string,assetClass="EQUITY",exchange="NSE",available=true):MarketRow=>
 ({symbol,available,instrument:{symbol,market:"INDIA",currency:"INR",assetClass,exchange}});
const group=(id:string,rows:MarketRow[])=>{
 const found=indiaCoverage(rows).groups.find(item=>item.id===id);
 assert.ok(found,id);
 return found;
};
const declared=(id:string)=>{
 const found=INDIA_COVERAGE_GROUPS.find(item=>item.id===id);
 assert.ok(found,id);
 return found.examples;
};

test("a group with collector rows lists the polled symbols in collector order, once each",()=>{
 const rows=[row("INFY"),row("LT"),row("TRENT"),row("BAJAJ-AUTO"),row("TRENT"),row("GOLDBEES","ETF"),row("NIFTY 50","INDEX")];
 const cash=group("nse-cash",rows);
 assert.deepEqual(cash.examples,["INFY","LT","TRENT","BAJAJ-AUTO"]);
 assert.equal(cash.status,"observed");
 const indexEtf=group("nse-index-etf",rows);
 assert.deepEqual(indexEtf.examples,["GOLDBEES","NIFTY 50"]);
 assert.equal(indexEtf.status,"observed");
});

test("a group with no collector rows keeps its declared examples and stays planned",()=>{
 const rows=[row("INFY")];
 const bse=group("bse-cash",rows);
 assert.deepEqual(bse.examples,declared("bse-cash"));
 assert.equal(bse.status,"planned");
 for(const item of indiaCoverage([]).groups){
  assert.deepEqual(item.examples,declared(item.id),item.id);
  assert.equal(item.status,"planned",item.id);
 }
});

test("a row that failed this cycle still names a polled symbol but cannot make the group observed",()=>{
 const failedOnly=group("nse-cash",[row("TRENT","EQUITY","NSE",false)]);
 assert.deepEqual(failedOnly.examples,["TRENT"]);
 assert.equal(failedOnly.status,"planned");
 const mixed=group("nse-cash",[row("TRENT","EQUITY","NSE",false),row("BEL")]);
 assert.deepEqual(mixed.examples,["TRENT","BEL"]);
 assert.equal(mixed.status,"observed");
});

test("rows outside a group's exchange, currency or market never reach it",()=>{
 const rows=[
  row("RELIANCE","EQUITY","BSE"),
  {symbol:"AAPL",available:true,instrument:{symbol:"AAPL",market:"US",currency:"USD",assetClass:"EQUITY",exchange:"NSE"}} as MarketRow,
  {symbol:"INFY",available:true} as MarketRow,
 ];
 const cash=group("nse-cash",rows);
 assert.deepEqual(cash.examples,declared("nse-cash"));
 assert.equal(cash.status,"planned");
 assert.deepEqual(group("bse-cash",rows).examples,["RELIANCE"]);
});
