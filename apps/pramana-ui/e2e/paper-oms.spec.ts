import {test,expect} from "@playwright/test";

const secret="synthetic-browser-fixture-secret-at-least-32-characters";
async function activity(page:import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("Workspace access key").fill(secret);
  await page.getByRole("button",{name:/Open workspace/}).click();
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("navigation",{name:"Main navigation"}).getByRole("button",{name:/Activity/}).click();
  await expect(page.getByRole("heading",{name:"Paper order recovery",exact:true})).toBeVisible();
}

test("authenticated OMS observation reaches Activity without granting recovery actions",async({page,request})=>{
  expect((await request.get("/api/paper-oms")).status()).toBe(401);
  const errors:string[]=[];page.on("pageerror",error=>errors.push(error.message));
  await activity(page);
  const panel=page.getByRole("region",{name:"Paper order recovery inspection"});
  await expect(panel.getByText("SUBMISSION_UNCERTAIN",{exact:true})).toBeVisible();
  await expect(panel.getByText("INFY · BUY",{exact:true})).toBeVisible();
  await expect(panel.getByText(/1 open stored orders/)).toBeVisible();
  await expect(panel.getByText(/cannot submit, cancel, retry, recover a trade or clear a halt/)).toBeVisible();
  const response=await page.request.get("/api/paper-oms");
  expect(response.status()).toBe(200);expect(response.headers()["cache-control"]).toBe("no-store");
  const body=await response.json();
  expect(body.totalOrders).toBe(1);expect(body.recoveryAuthorized).toBe(false);expect(body.historyVerified).toBe(false);
  expect(JSON.stringify(body)).not.toMatch(/other-tenant|OTHER-TENANT-PRIVATE/);
  expect((await page.request.get("/api/paper-oms?tenant=other-tenant")).status()).toBe(400);
  const origin=new URL(page.url()).origin;
  expect((await page.request.post("/api/paper-oms",{headers:{origin},data:{action:"recover"}})).status()).toBe(405);
  expect(await panel.getByRole("button").count()).toBe(1);
  await page.screenshot({path:"test-results/paper-oms-desktop.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("mobile inspection fits and failed refresh does not retain an old apparent result",async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await activity(page);
  const panel=page.getByRole("region",{name:"Paper order recovery inspection"});
  await expect(panel.getByText("SUBMISSION_UNCERTAIN",{exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:"test-results/paper-oms-mobile.png",fullPage:true});
  await page.route("**/api/paper-oms",route=>route.abort());
  await panel.getByRole("button",{name:"Refresh paper orders"}).click();
  await expect(panel.getByText(/observation is unavailable/)).toBeVisible();
  await expect(panel.getByText("SUBMISSION_UNCERTAIN",{exact:true})).toHaveCount(0);
});
