import fs from "node:fs";
import {defineConfig} from "@playwright/test";
import path from "node:path";
import os from "node:os";
import {spawnSync} from "node:child_process";

const externalFixture=path.resolve("tests/fixtures/external-account-snapshot.json");
const externalRef=JSON.parse(fs.readFileSync(externalFixture,"utf8")).accountRef as string;
// A fresh real-schema fixture for this test process, never a user's running ledger.
const paperOmsRoot=fs.mkdtempSync(path.join(os.tmpdir(),"pramana-browser-oms-"));
const seeded=spawnSync("python3",[path.resolve("e2e/seed-paper-oms.py"),paperOmsRoot],{encoding:"utf8",env:{...process.env,TRADING_LIVE_MONEY_ACTIVE:"false"}});
if(seeded.status!==0)throw new Error("Disposable paper-OMS browser fixture could not be created");
process.once("exit",()=>fs.rmSync(paperOmsRoot,{recursive:true,force:true}));
// The new console test uses real Python recovery handlers and only disposable state.
const recoveryOwner=!process.env.PRAMANA_RECOVERY_BROWSER_ROOT;
const recoveryRoot=process.env.PRAMANA_RECOVERY_BROWSER_ROOT || fs.mkdtempSync(path.join(os.tmpdir(),"pramana-browser-recovery-"));
process.env.PRAMANA_RECOVERY_BROWSER_ROOT=recoveryRoot;
const recoveryPort=Number(process.env.PRAMANA_RECOVERY_BROWSER_PORT || 3218);
process.env.PRAMANA_RECOVERY_BROWSER_PORT=String(recoveryPort);
const python=process.env.PRAMANA_BROWSER_PYTHON || "python3";
if(recoveryOwner){
  const result=spawnSync(python,[path.resolve("e2e/serve-institutional-recovery.py"),recoveryRoot,"--seed"],{encoding:"utf8",env:{...process.env,TRADING_LIVE_MONEY_ACTIVE:"false"}});
  if(result.status!==0)throw new Error("Disposable real-API recovery fixture could not be created: "+result.stderr);
  process.once("exit",()=>fs.rmSync(recoveryRoot,{recursive:true,force:true}));
}
const port=Number(process.env.PRAMANA_BROWSER_TEST_PORT || 3217);
const baseURL=`http://127.0.0.1:${port}`;
export default defineConfig({
  testDir:"./e2e",
  workers:1,
  retries:0,
  timeout:45000,
  use:{baseURL,trace:"retain-on-failure",screenshot:"only-on-failure"},
  reporter:[["list"],["json",{outputFile:"test-results/browser-verification.json"}]],
  webServer:[{
    command:`${JSON.stringify(python)} ${JSON.stringify(path.resolve("e2e/serve-institutional-recovery.py"))} ${JSON.stringify(recoveryRoot)} --serve --port ${recoveryPort}`,
    url:`http://127.0.0.1:${recoveryPort}/health`, timeout:60000, reuseExistingServer:false,
    env:{TRADING_LIVE_MONEY_ACTIVE:"false"},
  },{
    command:`npm run start -- --hostname 127.0.0.1 --port ${port}`,
    url:`${baseURL}/login`,
    timeout:60000,
    reuseExistingServer:false,
    env:{
      PRAMANA_DASHBOARD_SECRET:"synthetic-browser-fixture-secret-at-least-32-characters",
      PRAMANA_PUBLIC_ORIGIN:baseURL,
      PRAMANA_OPERATOR_API_ORIGIN:`http://127.0.0.1:${recoveryPort}`,
      PRAMANA_OPERATOR_TENANT:"tenant",
      PRAMANA_OPERATOR_READ_KEY_FILE:path.join(recoveryRoot,"read.key"),
      PRAMANA_OPERATOR_APPLY_KEY_FILE:path.join(recoveryRoot,"apply.key"),
      PRAMANA_LEDGER_PATH:path.join(paperOmsRoot,"paper.sqlite"),
      PRAMANA_PROOF_DIR:path.resolve("tests/fixtures/specialist-proofs"),
      PRAMANA_OMS_DB:path.join(paperOmsRoot,"oms.sqlite"),
      PRAMANA_TENANT_ID:"default",
      PRAMANA_CONSOLE_DB:path.join(paperOmsRoot,"console.sqlite"),
      PRAMANA_BENCHMARK_ATTRIBUTION_REPORT:path.resolve("tests/fixtures/benchmark-attribution.json"),
      PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO:"example-portfolio",
      PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT:externalFixture,
      PRAMANA_EXTERNAL_ACCOUNT_REF:externalRef,
      PRAMANA_MARKET_SNAPSHOT:path.resolve("tests/fixtures/browser-market.json"),
      PRAMANA_ALERT_LOG:path.resolve("tests/fixtures/browser-alerts.jsonl"),
      TRADING_LIVE_MONEY_ACTIVE:"false",
    },
  }],
});
