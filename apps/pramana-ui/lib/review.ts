import fs from "node:fs";
import path from "node:path";
import { createHmac } from "node:crypto";
import { constantEqual } from "./auth";
import { ledgerPath, tenantId } from "./db";
export function reviewedGate(gate: "strategy" | "recovery"): {
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
