import {defineConfig} from "@playwright/test";
import path from "node:path";

const port=3217;
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
      PRAMANA_LEDGER_PATH:"/tmp/pramana-browser-fixture/pramana.db",
      PRAMANA_CONSOLE_DB:"/tmp/pramana-browser-fixture/console.sqlite",
      PRAMANA_BENCHMARK_ATTRIBUTION_REPORT:path.resolve("tests/fixtures/benchmark-attribution.json"),
      PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO:"example-portfolio",
      TRADING_LIVE_MONEY_ACTIVE:"false",
    },
  },
});
