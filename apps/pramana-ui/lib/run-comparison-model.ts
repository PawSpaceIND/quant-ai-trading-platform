export type RunPoint={at:string;ledgerId:number;equity:string|null;cash:string;holdings:{key:string;quantity:number;average:string;mark:string}[];issues:string[];strategySha256:string|null;strategyEligible:boolean};
export type RunMinute={minute:string;paper:RunPoint|null;replay:RunPoint|null;skewSeconds:number|null;issues:string[];equityDifference:string|null};
export type RunFill={ledgerId:number;orderId:string;key:string;symbol:string;side:"BUY"|"SELL";quantity:number;price:string;cashFees:string;at:string};
export type FillTotals={quantity:number;notional:string;cashFees:string;fillCount:number;averagePrice:string};
export type FillGroup={minute:string;key:string;symbol:string;side:"BUY"|"SELL";paper:FillTotals|null;replay:FillTotals|null;quantityDifference:number;priceDifference:string|null};
export type RunMetrics={paperReturn:string;replayReturn:string;returnDifference:string;endingEquityDifference:string;maxAbsoluteEquityDifference:string};
export type RunComparisonReport={schema:"pramana.run_comparison.v1";tenantId:string;generatedAt:string;qualification:"unqualified_paper_replay_diagnostic";automaticPromotion:false;
 window:{start:string;end:string;maxSkewSeconds:number;calendarSha256:string;excludedClosedMinutes:number};
 sources:Record<"paper"|"replay",{tenant:string;startingCapital:string;sourceSha256:string}>;
 configuration:{observedManifestHashes:string[];observedUnqualifiedPoints:number;replayRunId:string;replayDatasetSha256:string;sourceComparison:string;differentComponents:string[];strategyEquivalence:"unverified";replayProtectionModel:string};
 initialState:"unavailable"|"same_recorded_book"|"different_recorded_book";status:"complete_observations"|"incomplete_observations";
 metrics:RunMetrics|null;curve:RunMinute[];fills:Record<"paper"|"replay",RunFill[]>;fillGroups:FillGroup[];limitations:string[]};
export type RunComparisonState={status:"unavailable"|"invalid"|"published";detail:string;sha256?:string;report:RunComparisonReport|null};

// Independent regular-session calendar check; changes require an explicit new calendar qualification.
const closed=new Set(["01-15","01-26","03-03","03-26","03-31","04-03","04-14","05-01","05-28","06-26","09-14","10-02","10-20","11-10","11-24","12-25"].map(d=>`2026-${d}`));
export function regularMinute(ms:number) {
 const local=new Date(ms+330*60000),date=local.toISOString().slice(0,10),minute=local.getUTCHours()*60+local.getUTCMinutes();
 return local.getUTCFullYear()===2026&&!closed.has(date)&&(![0,6].includes(local.getUTCDay())||date==="2026-02-01")&&minute>=555&&minute<930;
}
export function metrics(curve:RunMinute[]) {
 if(curve.length<2||curve.some(p=>p.issues.length))return null;
 const first=curve[0],last=curve.at(-1)!;
 const paper=Number(last.paper!.equity)/Number(first.paper!.equity)-1,replay=Number(last.replay!.equity)/Number(first.replay!.equity)-1;
 return {paperReturn:paper,replayReturn:replay,returnDifference:paper-replay,endingEquityDifference:Number(last.paper!.equity)-Number(last.replay!.equity),maxAbsoluteEquityDifference:Math.max(...curve.map(p=>Math.abs(Number(p.paper!.equity)-Number(p.replay!.equity))))};
}
export function groupFills(fills:RunComparisonReport["fills"]):FillGroup[] {
 const groups=new Map<string,FillGroup>();
 for(const mode of ["paper","replay"] as const)for(const f of fills[mode]) {
  const minute=new Date(Math.floor(Date.parse(f.at)/60000)*60000).toISOString().replace(".000Z","+00:00"),key=JSON.stringify([minute,f.key,f.side]);
  const g=groups.get(key)??{minute,key:f.key,symbol:f.symbol,side:f.side,paper:null,replay:null,quantityDifference:0,priceDifference:null};
  const t=g[mode]??{quantity:0,notional:"0",cashFees:"0",fillCount:0,averagePrice:"0"};
  t.quantity+=f.quantity;t.notional=String(Number(t.notional)+f.quantity*Number(f.price));t.cashFees=String(Number(t.cashFees)+Number(f.cashFees));t.fillCount++;t.averagePrice=String(Number(t.notional)/t.quantity);g[mode]=t;groups.set(key,g);
 }
 return [...groups.entries()].sort(([a],[b])=>a<b?-1:a>b?1:0).map(([,g])=>({...g,quantityDifference:(g.paper?.quantity??0)-(g.replay?.quantity??0),priceDifference:g.paper&&g.replay?String(Number(g.paper.averagePrice)-Number(g.replay.averagePrice)):null}));
}
