import {test,expect} from "@playwright/test";

const secret="synthetic-browser-fixture-secret-at-least-32-characters";
async function login(page:import("@playwright/test").Page){
  await page.goto("/login");
  await page.getByLabel("Workspace access key").fill(secret);
  await page.getByRole("button",{name:/Open workspace/}).click();
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("navigation",{name:"Main navigation"}).getByRole("button",{name:/Research/}).click();
  await expect(page.getByRole("heading",{name:"Benchmark return attribution"})).toBeVisible();
}

test("private attribution flows through dashboard, export and Atlas",async({page})=>{
  const errors:string[]=[];
  page.on("pageerror",error=>errors.push(error.message));
  await login(page);
  await expect(page.getByText("example-portfolio versus example-benchmark",{exact:false})).toBeVisible();
  await expect(page.getByText("Historical calculation",{exact:true})).toBeVisible();
  await expect(page.getByText("Source quality, income and cost treatment remain unverified.",{exact:false})).toBeVisible();
  const downloadPromise=page.waitForEvent("download");
  await page.getByRole("link",{name:"Download attribution JSON"}).click();
  const download=await downloadPromise;
  expect(download.suggestedFilename()).toBe("pramana-benchmark-attribution.json");
  await page.getByRole("button",{name:"Ask Atlas about attribution"}).click();
  await expect(page.locator(".copilot-dock textarea")).toHaveValue(/example-portfolio/);
  await page.screenshot({path:"test-results/attribution-desktop.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("attribution remains usable in a 390px viewport",async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await login(page);
  const width=await page.evaluate(()=>document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(390);
  await expect(page.getByRole("link",{name:"Download attribution JSON"})).toBeVisible();
  await page.screenshot({path:"test-results/attribution-mobile.png",fullPage:true});
});


test("selected external account remains historical and private",async({page})=>{
 await page.goto("/login");
 await page.getByLabel("Workspace access key").fill(secret);
 await page.getByRole("button",{name:/Open workspace/}).click();
 await expect(page).toHaveURL(/\/$/);
 await page.getByRole("navigation",{name:"Main navigation"}).getByRole("button",{name:/Activity/}).click();
 await expect(page.getByRole("heading",{name:"Selected account funds & net positions"})).toBeVisible();
 await expect(page.getByText("Historical selected-account snapshot",{exact:false})).toBeVisible();
 await expect(page.getByRole("rowheader",{name:"INFY"})).toBeVisible();
 const downloadPromise=page.waitForEvent("download");
 await page.getByRole("link",{name:"Download selected account JSON"}).click();
 expect((await downloadPromise).suggestedFilename()).toBe("pramana-external-account.json");
 await page.getByRole("button",{name:"Ask Atlas about this account"}).click();
 await expect(page.locator(".copilot-dock textarea")).toHaveValue(/separate paper ledger/);
 await page.screenshot({path:"test-results/external-account-desktop.png",fullPage:true});
});

test("private market watchlist persists and hands a selected instrument to Atlas",async({page,request})=>{
 const errors:string[]=[];
 page.on("pageerror",error=>errors.push(error.message));
 expect((await request.get("/api/watchlist")).status()).toBe(401);
 expect((await request.put("/api/watchlist",{data:{symbols:["NSE:INFY"]}})).status()).toBe(403);
 await page.goto("/login");
 await page.getByLabel("Workspace access key").fill(secret);
 await page.getByRole("button",{name:/Open workspace/}).click();
 await page.getByRole("navigation",{name:"Main navigation"}).getByRole("button",{name:/Markets/}).click();
 await expect(page.getByRole("heading",{name:/Market watch/})).toBeVisible();
 await expect(page.getByText("Collector stale",{exact:true})).toBeVisible();
 await page.getByRole("textbox",{name:"Search instruments"}).fill("INFY");
 await expect(page.getByRole("button",{name:"NSE:INFY NSE · INR"})).toBeVisible();
 await expect(page.getByRole("button",{name:"NSE:TCS NSE · INR"})).toHaveCount(0);
 await page.getByRole("button",{name:"Save NSE:INFY"}).click();
 await expect(page.getByRole("button",{name:"Remove NSE:INFY"})).toBeVisible();
 await page.reload();
 await expect(page.getByRole("button",{name:"Remove NSE:INFY"})).toBeVisible();
 await page.getByRole("button",{name:"Ask Atlas ↗"}).click();
 await expect(page.locator(".copilot-dock textarea")).toHaveValue(/NSE:INFY/);
 await page.screenshot({path:"test-results/market-watch-atlas.png",fullPage:true});
 expect(errors).toEqual([]);
});
