import {test,expect,type Page} from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
const secret="synthetic-browser-fixture-secret-at-least-32-characters";
const fixture=()=>JSON.parse(fs.readFileSync(path.join(process.env.PRAMANA_RECOVERY_BROWSER_ROOT!,"fixture.json"),"utf8")) as {programId:string};
const source=()=>`http://127.0.0.1:${process.env.PRAMANA_RECOVERY_BROWSER_PORT}`;
async function activity(page:Page,login=true){
  if(login){await page.goto("/login");await page.getByLabel("Workspace access key").fill(secret);await page.getByRole("button",{name:/Open workspace/}).click();}
  await expect(page).toHaveURL(/\/(?:\?view=activity)?$/);
  await page.getByRole("navigation",{name:"Main navigation"}).getByRole("button",{name:/Activity/}).click();
  return page.getByRole("region",{name:"Institutional bookkeeping recovery"});
}
async function inspect(page:Page){
  const panel=await activity(page);await panel.getByLabel("Execution programme ID").fill(fixture().programId);
  await panel.getByRole("button",{name:"Inspect saved programme",exact:true}).click();
  await expect(panel.getByText("Recovery account: tenant",{exact:true})).toBeVisible();
  return panel;
}
test.describe.configure({mode:"serial"});
test("founder session explicitly confirms real API recovery without another trade",async({page,request})=>{
  expect((await request.get("/api/institutional-recovery?program_id=x")).status()).toBe(401);
  const before=await (await request.get(source()+"/fixture/observations")).json();
  expect(before).toMatchObject({brokerOrders:1,cash:"100000",securities:"0",auditEvents:0,haltEngaged:true});
  const errors:string[]=[];page.on("pageerror",e=>errors.push(e.message));
  const panel=await inspect(page);
  const apply=panel.getByRole("button",{name:"Confirm bookkeeping reconciliation",exact:true});
  await expect(apply).toBeDisabled();await panel.getByRole("checkbox").check();await apply.click();
  await expect(panel.getByTestId("recovery-outcome")).toContainText("Audit status: RETURNED");
  await expect(panel.getByTestId("recovery-outcome")).toContainText("Reconciliation stage: COMPLETE");
  expect(await (await request.get(source()+"/fixture/observations")).json()).toMatchObject({brokerOrders:1,cash:"99000",securities:"1000",auditEvents:2,haltEngaged:true});
  const html=await page.content();
  for(const name of ["read.key","apply.key"])expect(html).not.toContain(fs.readFileSync(path.join(process.env.PRAMANA_RECOVERY_BROWSER_ROOT!,name),"utf8"));
  await expect(apply).toBeDisabled();await panel.getByRole("button",{name:"Check saved outcome"}).click();
  await expect(panel.getByTestId("recovery-outcome")).toContainText("RETURNED");
  expect((await (await request.get(source()+"/fixture/observations")).json()).auditEvents).toBe(2);
  await page.screenshot({path:"test-results/institutional-recovery-desktop.png",fullPage:true});expect(errors).toEqual([]);
});
test("lost response preserves unknown request through reload and checks outcome with no retry",async({page,request})=>{
  const before=await (await request.get(source()+"/fixture/observations")).json();
  let posts=0;
  await page.route("**/api/institutional-recovery",async route=>{
    if(route.request().method()==="POST"){posts++;await route.fetch();await route.abort();}else await route.continue();
  });
  let panel=await inspect(page);await panel.getByRole("checkbox").check();
  await panel.getByRole("button",{name:"Confirm bookkeeping reconciliation"}).click();
  await expect(panel.getByText("Outcome not confirmed",{exact:true})).toBeVisible();
  await expect(panel.getByRole("alert")).toBeVisible();
  const id=await panel.getByTestId("recovery-request-id").innerText();expect(posts).toBe(1);
  await page.reload();panel=await activity(page,false);
  await expect(panel.getByTestId("recovery-request-id")).toHaveText(id);
  await panel.getByRole("button",{name:"Check saved outcome"}).click();
  await expect(panel.getByTestId("recovery-outcome")).toContainText("RETURNED");
  expect(posts).toBe(1);
  const after=await (await request.get(source()+"/fixture/observations")).json();
  expect(after.brokerOrders).toBe(before.brokerOrders);expect(after.auditEvents).toBe(before.auditEvents+2);expect(after.haltEngaged).toBe(true);
});
test("mobile failed refresh removes the old actionable preview",async({page})=>{
  await page.setViewportSize({width:390,height:844});const panel=await inspect(page);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:"test-results/institutional-recovery-mobile.png",fullPage:true});
  await page.route("**/api/institutional-recovery?program_id=*",route=>route.abort());
  await panel.getByRole("button",{name:"Inspect saved programme"}).click();
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(panel.getByRole("button",{name:"Confirm bookkeeping reconciliation"})).toHaveCount(0);
});

test("damaged local request tracking permits inspection but never replacement submission",async({page})=>{
  await page.addInitScript(()=>sessionStorage.setItem("pramana.bookkeeping-recovery.attempt.v1", "{invalid"));
  let posts=0;await page.route("**/api/institutional-recovery",async route=>{if(route.request().method()==="POST")posts++;await route.continue();});
  const panel=await inspect(page);
  await expect(panel.getByRole("checkbox")).toBeDisabled();
  await expect(panel.getByRole("button",{name:"Confirm bookkeeping reconciliation"})).toBeDisabled();
  expect(posts).toBe(0);
});
