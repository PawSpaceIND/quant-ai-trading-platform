import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, mkdirSync, copyFileSync, readFileSync, writeFileSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root=resolve(dirname(fileURLToPath(import.meta.url)),"..");
const parser="lib/regime-observation.ts";
const guards:Array<[string,string,string,string]>=[
  ["schema",parser,'value.schema !== "pramana.regime_observation.v1" || ',""],
  ["state",parser,'value.state !== "observed" ||',"false ||"],
  ["technical timeframe",parser,'value.technicalTimeframe !== "1m" || ',""],
  ["integer count",parser,'Number.isSafeInteger(value) && ',""],
  ["count ceiling",parser,' && value <= 1000000',""],
  ["positive minimum",parser,' || value.minimumBars < 1',""],
  ["minimum vs lookback",parser,'value.lookbackBars < value.minimumBars || ',""],
  ["timestamp validity",parser,'age === null || age < 0','age !== null && age < 0'],
  ["future timestamp",parser,'age === null || age < 0','age === null'],
  ["frame timeframe",parser,'raw.timeframe !== timeframe || ',""],
  ["label allowlist",parser,' || !labels.has(raw.label)',""],
  ["actual scored count",parser,'raw.barsUsed !== Math.min(raw.barsAvailable, value.lookbackBars as number) ||',"false ||"],
  ["label classified agreement",parser,'raw.classified !== (raw.label !== "insufficient_history") ||',"false ||"],
  ["classified count agreement",parser,'raw.classified !== (raw.barsUsed >= (value.minimumBars as number))','false'],
  ["selected timeframe",parser,'value.selectedTimeframe !== selected.timeframe || ',""],
  ["selected label",parser,'value.selectedLabel !== selected.label || ',""],
  ["selection reason",parser,'value.selectionReason !== reason','false'],
  ["actual dashboard hook","components/engine-feed-status.tsx",'    {rows.length ? <RegimeContextTable rows={rows} /> : null}',""],
];
const files=[parser,"lib/freshness.ts","components/regime-context-table.tsx","components/engine-feed-status.tsx","tests/regime-observation.test.ts"];
for(const [name,path,original,replacement] of guards) test("regime display guard detects removal: "+name,()=>{
  const folder=mkdtempSync(join(tmpdir(),"regime-display-guard-"));
  const before=readFileSync(join(root,path),"utf8");
  assert.equal(before.split(original).length,2,"guard must have one exact source anchor");
  try {
    for(const file of files){mkdirSync(dirname(join(folder,file)),{recursive:true});copyFileSync(join(root,file),join(folder,file));}
    mkdirSync(join(folder,"home"));
    symlinkSync(join(root,"node_modules"),join(folder,"node_modules"),"dir");
    writeFileSync(join(folder,"tsconfig.json"),JSON.stringify({compilerOptions:{jsx:"react-jsx",module:"ESNext",moduleResolution:"Bundler"}}));
    const run=()=>spawnSync(process.execPath,["--import","tsx","--test","--test-reporter=tap","tests/regime-observation.test.ts"],{
      cwd:folder,env:{PATH:process.env.PATH,HOME:join(folder,"home"),NODE_ENV:"test",TRADING_LIVE_MONEY_ACTIVE:"false"},encoding:"utf8",timeout:30000,
    });
    const control=run(); assert.equal(control.status,0,control.stdout+control.stderr); assert.match(control.stdout,/# fail 0/);
    writeFileSync(join(folder,path),before.replace(original,replacement));
    const changed=run(); assert.equal(changed.status,1,changed.stdout+changed.stderr);
    assert.match(changed.stdout,/ERR_ASSERTION|AssertionError/);
    assert.doesNotMatch(changed.stdout+changed.stderr,/SyntaxError|ReferenceError|TypeError|ERR_MODULE_NOT_FOUND/);
  } finally {
    rmSync(folder,{recursive:true,force:true});
    assert.equal(readFileSync(join(root,path),"utf8"),before);
  }
});
