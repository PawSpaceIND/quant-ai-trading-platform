import {test, expect} from "@playwright/test";

for (const width of [1440, 390]) {
  test(`specialist scope and source failure explain zero scores at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height:900});
    const errors:string[]=[];
    page.on("pageerror",e=>errors.push(e.message));
    await page.goto("/login");
    await page.getByLabel("Workspace access key").fill("synthetic-browser-fixture-secret-at-least-32-characters");
    await page.getByRole("button",{name:/Open workspace/}).click();
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
