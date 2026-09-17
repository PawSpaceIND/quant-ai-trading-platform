// Assertions for the offline container fixture, not a production risk policy.
import assert from "node:assert/strict";

export function assertContainerFixtureRiskGates(payload) {
  assert.equal(payload.schema, "pramana.risk_gates.v1");
  assert.ok(payload.gates.length >= 7);
  for (const item of payload.gates) {
    assert.equal(typeof item.armed, "boolean", item.id);
    assert.match(item.setting, /^PRAMANA_/);
  }
  // The fixture loads the five-name map, but never fetches historical data.
  // Preserve that distinction rather than making an unarmed fixture appear live-ready.
  assert.deepEqual(payload.gates.filter(item => item.armed).map(item => item.id),
    ["sector_concentration"]);
  const sector = payload.gates.find(item => item.id === "sector_concentration");
  assert.equal(sector.records, 5);
  assert.equal(sector.groups, 3);
  assert.equal(sector.limit, 0.25);
  for (const id of ["correlation_adjusted_gross", "book_expected_shortfall"]) {
    const gate = payload.gates.find(item => item.id === id);
    assert.ok(gate, id);
    // The exact armed-id assertion above already requires both history gates false.
  }
}
