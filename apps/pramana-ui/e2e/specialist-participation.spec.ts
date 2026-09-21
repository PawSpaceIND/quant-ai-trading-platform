import {test, expect} from "@playwright/test";
import {makeSession, SESSION_COOKIE} from "../lib/auth";

for (const width of [1440, 390]) {
  test(`specialist scope and source failure explain zero scores at ${width}px`, async ({page, context, baseURL}) => {
    await page.setViewportSize({width, height:900});
    const errors:string[]=[];
    page.on("pageerror",e=>errors.push(e.message));
    // Display tests use a locally signed fixture session, preserving the real
    // login endpoint's shared ten-per-minute cap and existing login coverage.
    const previous = process.env.PRAMANA_DASHBOARD_SECRET;
    let session: string;
    try {
      process.env.PRAMANA_DASHBOARD_SECRET = "synthetic-browser-fixture-secret-at-least-32-characters";
      session = makeSession();
    } finally {
      if (previous === undefined) delete process.env.PRAMANA_DASHBOARD_SECRET;
      else process.env.PRAMANA_DASHBOARD_SECRET = previous;
    }
    await context.addCookies([{name:SESSION_COOKIE,value:session,url:baseURL!,httpOnly:true,sameSite:"Strict"}]);
    await page.goto("/");
    await expect(page).toHaveURL(/\/$/);
    await page.getByRole("navigation",{name:"Main navigation"}).getByRole("button",{name:/Research/}).click();
    const panel = page.locator("section").filter({has:page.getByRole("heading",{name:"Swarm intelligence",exact:true})});
    await expect(panel.getByText("Company valuation does not apply to this asset type.",{exact:false})).toBeVisible();
    await expect(panel.getByText("No indicative fund-value source is configured.",{exact:false})).toBeVisible();
    await expect(panel.getByText("Recorded issue: The provider rejected authentication or access.",{exact:true})).toBeVisible();
    await expect(panel.getByText("Not applicable",{exact:true})).toHaveCount(3);
    expect(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true);
    await panel.screenshot({path:`test-results/specialist-participation-${width}.png`});
    expect(errors).toEqual([]);
  });
}
