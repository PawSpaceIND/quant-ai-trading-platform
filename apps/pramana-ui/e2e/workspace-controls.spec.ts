import {test, expect, type BrowserContext} from "@playwright/test";
import {makeSession, SESSION_COOKIE} from "../lib/auth";

async function signIn(context: BrowserContext, baseURL: string) {
  const previous = process.env.PRAMANA_DASHBOARD_SECRET;
  let session: string;
  try {
    process.env.PRAMANA_DASHBOARD_SECRET = "synthetic-browser-fixture-secret-at-least-32-characters";
    session = makeSession();
  } finally {
    if (previous === undefined) delete process.env.PRAMANA_DASHBOARD_SECRET;
    else process.env.PRAMANA_DASHBOARD_SECRET = previous;
  }
  await context.addCookies([{name: SESSION_COOKIE, value: session, url: baseURL, httpOnly: true, sameSite: "Strict"}]);
}

for (const width of [1280, 390]) {
  test(`market detail and Atlas question follow visible search results at ${width}px`, async ({page, context, baseURL}) => {
    await signIn(context, baseURL!);
    await page.setViewportSize({width, height: 720});
    let paidRequests = 0;
    await page.route("**/api/copilot", async route => {
      if (route.request().method() !== "GET") paidRequests++;
      await route.fulfill({json: {conversations: [], dailyRemaining: 10, dailyLimit: 10}});
    });
    await page.goto("/?view=markets");
    await page.getByRole("button", {name: "NSE:INFY NSE · INR · EQUITY", exact: true}).click();
    await expect(page.getByRole("heading", {name: "NSE:INFY", exact: true})).toBeVisible();
    await page.getByRole("textbox", {name: "Search instruments", exact: true}).fill("TCS");
    await expect(page.getByRole("heading", {name: "NSE:TCS", exact: true})).toBeVisible();
    await expect(page.getByRole("heading", {name: "NSE:INFY", exact: true})).toHaveCount(0);
    await page.getByRole("button", {name: "Ask Atlas ↗", exact: true}).click();
    await expect(page.getByRole("textbox", {name: "Ask Atlas", exact: true})).toHaveValue(/TCS/);
    await page.getByRole("button", {name: width < 1100 ? "Close copilot ×" : "✳ Atlas copilot", exact: true}).click();
    await page.getByRole("textbox", {name: "Search instruments", exact: true}).fill("NO-MATCH");
    await expect(page.getByText("No instruments match this filter.", {exact: true})).toBeVisible();
    await expect(page.getByRole("button", {name: "Ask Atlas ↗", exact: true})).toHaveCount(0);
    expect(paidRequests).toBe(0);
  });

  test(`new conversation clears old URL and remains new on reopen at ${width}px`, async ({page, context, baseURL}) => {
    await signIn(context, baseURL!);
    await page.setViewportSize({width, height: 720});
    const id = "11111111-1111-4111-8111-111111111111";
    const conversation = {id, tenant: "default", prompt: "Synthetic saved question", answer: "Synthetic saved answer", status: "complete", model: "synthetic", usage: "{}", created_at: "2026-09-21T00:00:00Z"};
    let paidRequests = 0;
    await page.route("**/api/copilot", async route => {
      if (route.request().method() !== "GET") paidRequests++;
      await route.fulfill({json: {conversations: [conversation], dailyRemaining: 10, dailyLimit: 10}});
    });
    await page.route(`**/api/copilot/${id}`, route => route.fulfill({json: conversation}));
    await page.goto(`/?view=markets&chat=${id}`);
    await expect(page.getByText("Synthetic saved answer", {exact: true})).toBeVisible();
    await page.getByText("Saved conversations (1)", {exact: true}).click();
    await page.getByRole("button", {name: "+ New conversation", exact: true}).click();
    expect(new URL(page.url()).searchParams.has("chat")).toBe(false);
    expect(new URL(page.url()).searchParams.get("view")).toBe("markets");
    await page.getByRole("button", {name: width < 1100 ? "Close copilot ×" : "✳ Atlas copilot", exact: true}).click();
    await page.getByRole("button", {name: "✳ Atlas copilot", exact: true}).click();
    await expect(page.getByRole("heading", {name: "Start with a better question.", exact: true})).toBeVisible();
    await expect(page.getByText("Synthetic saved answer", {exact: true})).toHaveCount(0);
    expect(paidRequests).toBe(0);
  });
}

test("desktop chat history remains pointer reachable on a short screen", async ({page, context, baseURL}) => {
  await signIn(context, baseURL!);
  await page.setViewportSize({width: 1280, height: 720});
  await page.route("**/api/copilot", route => route.fulfill({json: {
    conversations: [{id: "synthetic", prompt: "Saved fixture", status: "complete"}],
    dailyRemaining: 10, dailyLimit: 10,
    dollarBudget: {status: "activation_hold", limitUsd: 2.5, remainingUsd: 0},
  }}));
  await page.goto("/?view=markets");
  await page.getByRole("button", {name: "✳ Atlas copilot", exact: true}).click();
  await page.getByText("Saved conversations (1)", {exact: true}).click();
  await expect(page.getByRole("button", {name: "+ New conversation", exact: true})).toBeVisible();
  const panel = page.locator(".copilot");
  expect(await panel.evaluate(el => el.getBoundingClientRect().height)).toBeLessThanOrEqual(680);
  expect(await panel.evaluate(el => getComputedStyle(el).overflowY)).toBe("auto");
});

for (const width of [1280, 390]) {
  test(`sign out is reachable and reports failures at ${width}px`, async ({page, context, baseURL}) => {
    await signIn(context, baseURL!);
    await page.setViewportSize({width, height: 844});
    await page.goto("/?view=markets");
    const logout = page.getByRole("button", {name: "↪ Sign out", exact: true});
    await expect(logout).toBeVisible();
    await page.route("**/api/session", route => route.fulfill({status: 503, json: {error: "Synthetic failure"}}));
    await logout.click();
    // The banner also carries the dismiss control, so match the status region, not the exact text.
    const failure = page.getByRole("status").filter({hasText: "Sign out could not be confirmed"});
    await expect(failure).toContainText("Your session may still be active; try Sign out again.");
    expect(new URL(page.url()).pathname).toBe("/");
    await page.unroute("**/api/session");
    await logout.click();
    await expect(page).toHaveURL(/\/login$/);
    await page.goto("/?view=quality");
    await expect(page).toHaveURL(/\/login$/);
  });
}

test("a refused halt reports inside the dialog instead of behind its backdrop", async ({page, context, baseURL}) => {
  await signIn(context, baseURL!);
  await page.setViewportSize({width: 1280, height: 900});
  await page.route("**/api/control", route => route.fulfill({status: 503, json: {error: "Synthetic control failure."}}));
  await page.goto("/?view=overview");
  await page.getByRole("button", {name: "Halt entries", exact: true}).click();
  const dialog = page.getByRole("dialog", {name: "Halt new paper entries?"});
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Reason").fill("synthetic browser check");
  await dialog.getByRole("button", {name: "Request halt", exact: true}).click();
  // The notice banner lives in the shell that is inert and covered while the dialog is
  // open, so a failure reported there is neither seen nor announced.
  const failure = dialog.getByRole("alert");
  await expect(failure).toContainText("The halt was not acknowledged. Entries are not halted.");
  await expect(dialog).toBeVisible();
  // Re-opening the dialog must not carry the previous failure back.
  await dialog.getByRole("button", {name: "Cancel", exact: true}).click();
  await page.getByRole("button", {name: "Halt entries", exact: true}).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toHaveCount(0);
});

test("the alerts the engine raised reach a screen", async ({page, context, baseURL}) => {
  await signIn(context, baseURL!);
  await page.goto("/?view=activity");
  const panel = page.getByRole("region", {name: "Engine alerts"});
  // Seven of the engine's codes had no path to any screen. MACRO_PROVIDER_UNAVAILABLE is
  // the one its own source calls out: a refused provider leaves every macro-reading
  // specialist at zero confidence and the pilot holding all day with nothing to show.
  await expect(panel.getByText("Macro provider unavailable", {exact: true})).toBeVisible();
  await expect(panel.getByText("Unexplained overnight gap", {exact: true})).toBeVisible();
  await expect(panel.getByText(/provider: FRED/)).toBeVisible();
  // Newest first, and another account's alert on the same shared volume is not shown.
  const labels = await panel.locator("tbody tr td:nth-child(3) strong").allInnerTexts();
  expect(labels[0]).toBe("Trading halted");
  expect(await panel.getByText(/OTHER-TENANT-PRIVATE/).count()).toBe(0);
});
