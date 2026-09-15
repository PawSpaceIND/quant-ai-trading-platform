import { strict as assert } from "node:assert";
import test from "node:test";
import { portfolioAttribution } from "../lib/portfolio-attribution";

const portfolio = {totalEquity: 1000, holdings: [{symbol: "INFY", marketValue: 600}, {symbol: "TCS", marketValue: 400}]} as any;
const metadata = JSON.stringify({schema: "pramana.risk_metadata.v1", asOf: "2026-09-15T00:00:00+00:00", symbols: {INFY: {sector: "Technology", factors: {market: 1.1}}, TCS: {sector: "Technology", factors: {market: 0.9}}}});

test("portfolio attribution aggregates reviewed sector and factor exposure", () => {
  const result = portfolioAttribution(portfolio, metadata);
  assert.equal(result.status, "available");
  assert.equal(result.sectors?.[0].name, "Technology");
  assert.equal(result.sectors?.[0].weight, 1);
  assert.equal(result.factors?.[0].exposure, 1.02);
});

test("portfolio attribution fails closed without complete mappings", () => {
  assert.equal(portfolioAttribution(portfolio).status, "unavailable");
  assert.equal(portfolioAttribution(portfolio, metadata.replace('"TCS"', '"MISSING"')).status, "unavailable");
  assert.equal(portfolioAttribution(portfolio, metadata.replace("2026-09-15T00:00:00+00:00", "2026-09-15T00:00:00")).status, "unavailable");
  assert.equal(portfolioAttribution(portfolio, metadata.replace('"symbols":', '"unexpected":true,"symbols":')).status, "unavailable");
});
