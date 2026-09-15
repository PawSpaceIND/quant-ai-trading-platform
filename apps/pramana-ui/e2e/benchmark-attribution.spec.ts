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
