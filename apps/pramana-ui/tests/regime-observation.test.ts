import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readRegimeObservation } from "../lib/regime-observation";
import { RegimeContextTable } from "../components/regime-context-table";
import { EngineFeedStatus } from "../components/engine-feed-status";
import type { Runtime } from "../lib/pilot";

const now=Date.parse("2026-09-17T10:30:00Z");
function sample() {
  return {schema:"pramana.regime_observation.v1",state:"observed",observedAt:"2026-09-17T10:29:00+00:00",ageSeconds:0,
    technicalTimeframe:"1m",technicalBars:60,minimumBars:20,lookbackBars:40,
    daily:{timeframe:"1d",label:"trending_up",barsUsed:40,barsAvailable:120,classified:true},
    intraday:{timeframe:"15m",label:"insufficient_history",barsUsed:4,barsAvailable:4,classified:false},
    selectedTimeframe:"1d",selectedLabel:"trending_up",selectionReason:"daily_classified"};
}
function rows(value:unknown):NonNullable<Runtime["watchlist"]> {
  return [{symbol:"INFY",market:"INDIA",exchange:"NSE",assetClass:"EQUITY",currency:"INR",fresh:false,regimeContext:value}];
}

test("parse real counts and recompute age rather than trust the stored counter",()=>{
  const parsed=readRegimeObservation(sample(),now);
  assert.equal(parsed?.technicalBars,60);
  assert.equal(parsed?.daily.barsUsed,40);
  assert.equal(parsed?.daily.barsAvailable,120);
  assert.equal(parsed?.intraday.barsUsed,4);
  assert.equal(parsed?.ageSeconds,60);
});

test("render actual scored/available counts and selection reason",()=>{
  const html=renderToStaticMarkup(createElement(RegimeContextTable,{rows:rows(sample()),now}));
  for(const text of ["Last analysed regime context","40 / 120 bars","4 / 4 bars","Technical 1m bars","trending_up","Minimum 20; lookback 40 bars","60s since context","Classified daily context takes priority"])
    assert.ok(html.includes(text),text);
});

test("the existing engine feed surface includes the new observation component",()=>{
  const runtime:Runtime={status:"running",mode:"paper",watchlist:rows(undefined)};
  const html=renderToStaticMarkup(createElement(EngineFeedStatus,{runtime,onAsk:()=>{}}));
  assert.ok(html.includes("Last analysed regime context"));
  assert.ok(html.includes("No valid recorded regime context"));
});

for(const value of [undefined,null,[],{}, {schema:"other",state:"observed"}, {...sample(),state:"not_observed"}, {...sample(),state:"busy"}, {...sample(),state:"unavailable"}]) {
  test("legacy/missing/unavailable snapshots do not invent regime counts "+JSON.stringify(value),()=>{
    assert.equal(readRegimeObservation(value,now),null);
    const html=renderToStaticMarkup(createElement(RegimeContextTable,{rows:rows(value),now}));
    assert.ok(html.includes("Counts and labels are unknown"));
    assert.ok(!html.includes("0 / 0 bars"));
  });
}

const changes:Array<[string,(x:ReturnType<typeof sample>)=>void]>=[
  ["future",x=>{x.observedAt="2026-09-17T10:30:00.000001Z";}],
  ["timezone missing",x=>{x.observedAt="2026-09-17T10:29:00";}],
  ["bad date",x=>{x.observedAt="2026-02-31T10:29:00Z";}],
  ["negative count",x=>{x.daily.barsUsed=-1;}],
  ["nonfinite count",x=>{x.daily.barsAvailable=NaN;}],
  ["fractional count",x=>{x.technicalBars=1.5;}],
  ["oversized count",x=>{x.technicalBars=1000001;}],
  ["scored differs",x=>{x.daily.barsUsed=39;}],
  ["wrong label",x=>{x.daily.label="made_up";}],
  ["wrong classified flag",x=>{x.daily.classified=false;}],
  ["wrong timeframe",x=>{x.daily.timeframe="1m";}],
  ["wrong selection",x=>{x.selectedTimeframe="15m";}],
  ["wrong reason",x=>{x.selectionReason="invented";}],
  ["wrong selected label",x=>{x.selectedLabel="ranging";}],
  ["technical timeframe",x=>{x.technicalTimeframe="15m";}],
  ["minimum invalid",x=>{x.minimumBars=0;}],
  ["lookback invalid",x=>{x.lookbackBars=19;}],
];
for(const [name,change] of changes) test("refuse contradictory evidence: "+name,()=>{
  const input=sample(); change(input); assert.equal(readRegimeObservation(input,now),null);
});

test("intraday fallback and insufficient-history tie remain explicit",()=>{
  const input=sample(); input.daily={timeframe:"1d",label:"insufficient_history",barsUsed:0,barsAvailable:0,classified:false};
  input.intraday={timeframe:"15m",label:"ranging",barsUsed:20,barsAvailable:20,classified:true};
  input.selectedTimeframe="15m"; input.selectedLabel="ranging"; input.selectionReason="intraday_fallback";
  assert.equal(readRegimeObservation(input,now)?.selectedTimeframe,"15m");
  input.intraday={timeframe:"15m",label:"insufficient_history",barsUsed:0,barsAvailable:0,classified:false};
  input.selectedTimeframe="1d"; input.selectedLabel="insufficient_history"; input.selectionReason="neither_classified_most_bars_daily_tiebreak";
  assert.equal(readRegimeObservation(input,now)?.selectedTimeframe,"1d");
});


test("schema identity is required even with otherwise consistent data",()=>{
  const x=sample(); x.schema="unrecognized"; assert.equal(readRegimeObservation(x,now),null);
});
test("unknown labels cannot self-certify by matching the selected label",()=>{
  const x=sample(); x.daily.label="invented"; x.selectedLabel="invented";
  assert.equal(readRegimeObservation(x,now),null);
});
test("label and classified flag must agree independently of the bar count",()=>{
  const x=sample(); x.daily.label="insufficient_history"; x.selectedLabel="insufficient_history";
  assert.equal(readRegimeObservation(x,now),null);
});
test("classified label cannot bypass the actual bar minimum",()=>{
  const x=sample(); x.intraday.label="ranging"; x.intraday.classified=true;
  assert.equal(readRegimeObservation(x,now),null);
});
test("invalid declared minimum cannot be hidden by consistent labels",()=>{
  const x=sample(); x.minimumBars=0; x.intraday.label="ranging"; x.intraday.classified=true;
  assert.equal(readRegimeObservation(x,now),null);
});
test("lookback cannot be below the declared minimum",()=>{
  const x=sample(); x.lookbackBars=19; x.daily.barsUsed=19; x.daily.label="insufficient_history"; x.daily.classified=false;
  x.selectedLabel="insufficient_history"; x.selectionReason="neither_classified_most_bars_daily_tiebreak";
  assert.equal(readRegimeObservation(x,now),null);
});
