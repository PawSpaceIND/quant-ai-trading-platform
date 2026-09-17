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
const port=Number(process.env.PRAMANA_BROWSER_TEST_PORT || 3217);
const baseURL=`http://127.0.0.1:${port}`;
export default defineConfig({
  testDir:"./e2e",
  workers:1,
  retries:0,
  timeout:45000,
  use:{baseURL,trace:"retain-on-failure",screenshot:"only-on-failure"},
  reporter:[["list"],["json",{outputFile:"test-results/browser-verification.json"}]],
  webServer:{
    command:`npm run start -- --hostname 127.0.0.1 --port ${port}`,
    url:`${baseURL}/login`,
    timeout:60000,
    reuseExistingServer:false,
    env:{
      PRAMANA_DASHBOARD_SECRET:"synthetic-browser-fixture-secret-at-least-32-characters",
      PRAMANA_PUBLIC_ORIGIN:baseURL,
      PRAMANA_LEDGER_PATH:path.join(paperOmsRoot,"paper.sqlite"),
      PRAMANA_OMS_DB:path.join(paperOmsRoot,"oms.sqlite"),
      PRAMANA_TENANT_ID:"default",
      PRAMANA_CONSOLE_DB:path.join(paperOmsRoot,"console.sqlite"),
      PRAMANA_BENCHMARK_ATTRIBUTION_REPORT:path.resolve("tests/fixtures/benchmark-attribution.json"),
      PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO:"example-portfolio",
      PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT:externalFixture,
      PRAMANA_EXTERNAL_ACCOUNT_REF:externalRef,
      PRAMANA_MARKET_SNAPSHOT:path.resolve("tests/fixtures/browser-market.json"),
      TRADING_LIVE_MONEY_ACTIVE:"false",
    },
  },
});
