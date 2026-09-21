import {test, expect} from "@playwright/test";
import {makeSession, SESSION_COOKIE} from "../lib/auth";

for (const width of [1440, 390]) {
  test(`shared AI budget is readable at ${width}px without a model call`, async ({page, context, baseURL}) => {
    await page.setViewportSize({width, height:900});
    const errors:string[]=[];
    page.on("pageerror", e=>errors.push(e.message));
    const previous=process.env.PRAMANA_DASHBOARD_SECRET;
    let session:string;
    try {
      process.env.PRAMANA_DASHBOARD_SECRET="synthetic-browser-fixture-secret-at-least-32-characters";
      session=makeSession();
    } finally {
      if(previous===undefined)delete process.env.PRAMANA_DASHBOARD_SECRET;
      else process.env.PRAMANA_DASHBOARD_SECRET=previous;
    }
    await context.addCookies([{name:SESSION_COOKIE,value:session,url:baseURL!,httpOnly:true,sameSite:"Strict"}]);
    await page.route("**/api/copilot", async route=>{
      expect(route.request().method()).toBe("GET");
      await route.fulfill({json:{conversations:[],dailyRemaining:10,dailyLimit:10,
        dollarBudget:{status:"available",limitUsd:2.50,spentUsd:0.80,reservedUsd:0.20,remainingUsd:1.50}}});
    });
    await page.goto("/");
    await page.getByRole("button",{name:"✳ Atlas copilot",exact:true}).click();
    const banner=page.locator(".chat-dollar-budget");
    await expect(banner).toContainText("Combined AI limit: $2.50/day");
    await expect(banner).toContainText("estimated used $0.80 · reserved $0.20 · available $1.50");
    await expect(banner).toContainText("05:30 IST");
    expect(await banner.evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true);
    await page.locator(".copilot-dock").screenshot({path:`test-results/ai-budget-${width}.png`});
    expect(errors).toEqual([]);
  });
}
