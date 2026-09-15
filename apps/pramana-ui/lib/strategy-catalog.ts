export type StrategyDescriptor = {
  id: string;
  label: string;
  products: string[];
  sides: string[];
  horizon: string;
  requiredEvidence: string[];
  status: "paper_only" | "research_only";
};

export const STRATEGY_CATALOG: StrategyDescriptor[] = [
  {id:"momentum.v1",label:"Time-series momentum",products:["equity","ETF","future","FX","commodity"],sides:["LONG","SHORT (research)"],horizon:"Swing",requiredEvidence:["Closed daily bars","Cost/slippage","Forward paper run"],status:"paper_only"},
  {id:"mean_reversion",label:"Mean reversion",products:["equity","ETF","future","FX"],sides:["LONG","SHORT (research)"],horizon:"Short swing",requiredEvidence:["Stationarity/threshold review","Cost/slippage","Forward paper run"],status:"paper_only"},
  {id:"breakout",label:"Volatility breakout",products:["equity","future","commodity","FX"],sides:["LONG","SHORT (research)"],horizon:"Intraday to swing",requiredEvidence:["Session-aware bars","Gap/slippage stress","Forward paper run"],status:"paper_only"},
  {id:"futures-trend-roll",label:"Futures trend with roll",products:["future"],sides:["LONG","SHORT (research)"],horizon:"Contract lifecycle",requiredEvidence:["Exact contract/expiry","Roll calendar","Margin and settlement","Forward paper run"],status:"research_only"},
  {id:"defined-risk-option-spread",label:"Defined-risk option spread",products:["option"],sides:["Debit spread","Credit spread (research)"],horizon:"Expiry-aware",requiredEvidence:["Exact legs/strikes","Greeks/IV source","Margin and assignment","Forward paper run"],status:"research_only"},
  {id:"covered-call-protective-put",label:"Covered call / protective put",products:["equity","option"],sides:["Covered","Protective"],horizon:"Portfolio hedge",requiredEvidence:["Underlying/leg linkage","Corporate actions","Assignment/settlement","Forward paper run"],status:"research_only"},
  {id:"pairs-relative-value",label:"Pairs and relative value",products:["equity","future","ETF"],sides:["Long/short (research)"],horizon:"Market-neutral research",requiredEvidence:["Pair linkage","Borrow/shortability","Leg synchronisation","Forward paper run"],status:"research_only"},
  {id:"commodity-calendar-spread",label:"Commodity calendar spread",products:["commodity","metal","future"],sides:["Long/short (research)"],horizon:"Contract spread",requiredEvidence:["Two exact expiries","Delivery/quality rules","Margin offsets","Forward paper run"],status:"research_only"},
];
