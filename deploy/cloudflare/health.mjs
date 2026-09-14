// Independently age the original engine evidence; upload activity is not liveness.
export function snapshotHealth(row, now = Date.now()) {
  const reasons = [];
  function age(value, label, maximum) {
    const timestamp = typeof value === "string" ? Date.parse(value) : NaN;
    const seconds = (now - timestamp) / 1000;
    if (!Number.isFinite(seconds) || seconds < -5 || seconds > maximum) {
      reasons.push(label);
    }
    return Number.isFinite(seconds) ? Math.round(seconds) : null;
  }
  const sourceAgeSeconds = age(row?.source_at, "publication_stale_or_invalid", 180);
  const receivedAgeSeconds = age(row?.received_at, "receipt_stale_or_invalid", 180);
  let snapshots;
  try { snapshots = JSON.parse(row?.body ?? "null"); } catch { /* fail closed below */ }
  const workspace = snapshots?.["/api/workspace"];
  const runtime = workspace?.runtime;
  const heartbeatAgeSeconds = age(runtime?.updatedAt, "engine_heartbeat_stale_or_invalid", 150);
  if (workspace?.tenantId !== "india-paper" || runtime?.mode !== "paper") {
    reasons.push("paper_engine_identity_missing");
  }
  if (runtime?.status !== "running") reasons.push("engine_not_reporting_running");
  // A halt may be deliberate, but it must not be hidden from an availability probe.
  if (runtime?.halted !== false) reasons.push("engine_halted_or_halt_state_unknown");
  return {
    status: reasons.length ? "unhealthy" : "observation_ok",
    reasons,
    sourceAgeSeconds,
    receivedAgeSeconds,
    heartbeatAgeSeconds,
    checkedAt: new Date(now).toISOString(),
    mode: "paper",
    scope: "Published engine evidence only; not current feed, execution or strategy readiness",
  };
}
