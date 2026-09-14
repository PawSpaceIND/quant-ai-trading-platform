import fs from "node:fs";
import path from "node:path";
import { createHash, createHmac } from "node:crypto";
import { constantEqual } from "./auth";
import { hasTable, ledgerPath, openLedger, tenantId } from "./db";
import { readRuntime, type Runtime } from "./pilot";

export function verifiedRuntimeManifest(runtime: Runtime): {pass: boolean; detail: string; sha256?: string} {
  const m = runtime.strategyManifest;
  const age = Date.now() - Date.parse(m?.checkedAt ?? "");
  const runtimeAge = Date.now() - Date.parse(runtime.updatedAt ?? "");
  const freshSource = typeof m?.sourceCheckAgeSeconds === "number" && m.sourceCheckAgeSeconds >= 0 && m.sourceCheckAgeSeconds <= 65;
  if (!Number.isFinite(runtimeAge) || runtimeAge < -5000 || runtimeAge > 10000
      || runtime.status !== "running" || m?.status !== "matched" || !m.sha256 || !/^[0-9a-f]{64}$/.test(m.sha256)
      || !/^[0-9a-f]{64}$/.test(m.sourceSha256 ?? "")
      || !/^[0-9a-f]{40}$/.test(m.releaseRevision ?? "") || m.bootSha256 !== m.sha256 || !freshSource || !Number.isFinite(age) || age < -5000 || age > 10000
      || !Array.isArray(m.issues) || m.issues.length || m.releaseRevision !== process.env.PRAMANA_RELEASE_REVISION) {
    return {pass:false,detail:`Engine strategy configuration is ${m?.status ?? "unrecorded"}; fresh, complete runtime binding is required.`};
  }
  let db: ReturnType<typeof openLedger> = null;
  try {
    db = openLedger();
    if (!db || !hasTable(db,"pilot_strategy_manifests")) throw new Error();
    const row = db.prepare("SELECT payload FROM pilot_strategy_manifests WHERE tenant_id=? AND sha256=?").get(tenantId,m.sha256) as {payload:string} | undefined;
    if (!row || createHash("sha256").update(row.payload).digest("hex") !== m.sha256) throw new Error();
    const record = JSON.parse(row.payload);
    if (record.schema !== "pramana.runtime_strategy.v1" || record.tenant_id !== tenantId
        || record.release_revision !== m.releaseRevision || record.source?.sha256 !== m.sourceSha256) throw new Error();
    return {pass:true,sha256:m.sha256,detail:`Engine configuration ${m.sha256.slice(0,12)} matches its startup manifest. Source is checked at least every 60 seconds and before submission.`};
  } catch {
    return {pass:false,detail:"Engine manifest is absent, corrupt or does not match the active record."};
  } finally { db?.close(); }
}

export function reviewedGate(gate: "strategy" | "recovery", runtime?: Runtime): {
  pass: boolean;
  detail: string;
} {
  try {
    const secret = process.env.PRAMANA_REVIEW_SECRET || "";
    const revision = process.env.PRAMANA_RELEASE_REVISION;
    if (secret.length < 32 || !revision)
      return {
        pass: false,
        detail: "Release-bound operator review not configured",
      };
    const directory =
      process.env.PRAMANA_REVIEW_DIR ||
      path.join(path.dirname(ledgerPath()), "reviews");
    const file = path.join(directory, `${gate}.json`);
    if (fs.statSync(/* turbopackIgnore: true */ file).size > 100000)
      throw new Error();
    const envelope = JSON.parse(
      fs.readFileSync(/* turbopackIgnore: true */ file, "utf8"),
    );
    if (
      typeof envelope.payload !== "string" ||
      typeof envelope.signature !== "string" ||
      !constantEqual(
        envelope.signature,
        createHmac("sha256", secret).update(envelope.payload).digest("hex"),
      )
    )
      throw new Error();
    const r = JSON.parse(envelope.payload);
    const expiry = Date.parse(r.expires_at),
      issued = Date.parse(r.reviewed_at);
    if (
      r.schema !== "pramana.pilot.acceptance.v1" ||
      r.scope !== "private-paper-pilot" ||
      r.gate !== gate ||
      r.tenant_id !== tenantId ||
      r.release_revision !== revision ||
      !r.reviewer ||
      !Number.isFinite(expiry) ||
      !Number.isFinite(issued) ||
      expiry <= Date.now() ||
      issued > Date.now() + 5000 ||
      expiry - issued > 7 * 86400000
    )
      throw new Error();
    if (
      gate === "strategy" &&
      (!process.env.PRAMANA_STRATEGY_CONFIG_SHA256 ||
        r.artifact?.strategy_config_sha256 !==
          process.env.PRAMANA_STRATEGY_CONFIG_SHA256)
    )
      throw new Error();
    if (gate === "strategy") {
      const active = verifiedRuntimeManifest(runtime ?? readRuntime());
      if (!active.pass || active.sha256 !== r.artifact?.strategy_config_sha256) {
        return {pass:false,detail:`Strategy review does not match verified running configuration. ${active.detail}`};
      }
    }
    return {
      pass: true,
      detail: `Operator attestation by ${r.reviewer}, ${r.reviewed_at}. Valid only for this release; not an independent certification.`,
    };
  } catch {
    return {
      pass: false,
      detail: "No valid, unexpired operator review for this release",
    };
  }
}
