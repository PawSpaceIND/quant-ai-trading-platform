import {test, expect} from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("actual recovery service excludes trading and model routes even with a valid recovery key", async ({request}) => {
  const root = process.env.PRAMANA_RECOVERY_BROWSER_ROOT!;
  const origin = `http://127.0.0.1:${process.env.PRAMANA_RECOVERY_BROWSER_PORT}`;
  const {programId} = JSON.parse(fs.readFileSync(path.join(root, "fixture.json"), "utf8"));
  const headers = {"X-API-Key": fs.readFileSync(path.join(root, "read.key"), "utf8")};
  const before = await (await request.get(origin + "/fixture/observations")).json();
  const preview = await request.get(origin + `/v1/institutional/programs/${programId}/recovery`, {headers});
  expect(preview.status()).toBe(200);
  expect((await preview.json()).execution_authorized).toBe(false);
  for (const route of ["/v1/paper/trades", "/v1/live/trades", "/v1/atlas/cycle", "/v1/capital/recommendation"]) {
    const response = await request.post(origin + route, {headers, data: {}});
    expect(response.status()).toBe(404);
    expect(response.headers()["cache-control"]).toBe("no-store");
  }
  for (const route of ["/docs", "/redoc", "/openapi.json", "/v1/portfolio", "/v1/readiness"]) {
    expect((await request.get(origin + route, {headers})).status()).toBe(404);
  }
  expect(await (await request.get(origin + "/health")).json()).toMatchObject({
    status: "recovery_only", trading_routes_available: false, live_execution_available: false,
    account_health_verified: false, automatic_recovery: false,
  });
  expect(await (await request.get(origin + "/fixture/observations")).json()).toEqual(before);
});
