import { after, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash, createHmac } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import type { Runtime } from "../lib/pilot";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pramana-manifest-test-"));
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "ledger.sqlite");
process.env.PRAMANA_REVIEW_DIR = dir;
process.env.PRAMANA_REVIEW_SECRET = "synthetic-review-key-not-production-123";
process.env.PRAMANA_RELEASE_REVISION = "a".repeat(40);
const sourceHash = "b".repeat(64);
const payload = JSON.stringify({schema:"pramana.runtime_strategy.v1",tenant_id:"default",release_revision:"a".repeat(40),source:{sha256:sourceHash}});
const sha = createHash("sha256").update(payload).digest("hex");
const db = new DatabaseSync(process.env.PRAMANA_LEDGER_PATH);
db.exec("CREATE TABLE pilot_strategy_manifests(tenant_id TEXT,sha256 TEXT,payload TEXT)");
db.prepare("INSERT INTO pilot_strategy_manifests VALUES ('default',?,?)").run(sha,payload);
after(() => { db.close(); fs.rmSync(dir,{recursive:true,force:true}); });
function fresh(): Runtime {
  return {status:"running",mode:"paper",updatedAt:new Date().toISOString(),strategyManifest:{status:"matched",sha256:sha,bootSha256:sha,sourceSha256:sourceHash,releaseRevision:"a".repeat(40),checkedAt:new Date().toISOString(),sourceCheckAgeSeconds:0,issues:[]}};
}
function sign(strategyHash=sha) {
  process.env.PRAMANA_STRATEGY_CONFIG_SHA256 = strategyHash;
  const body = JSON.stringify({schema:"pramana.pilot.acceptance.v1",scope:"private-paper-pilot",gate:"strategy",tenant_id:"default",release_revision:"a".repeat(40),reviewer:"Synthetic QA",reviewed_at:new Date().toISOString(),expires_at:new Date(Date.now()+86400000).toISOString(),artifact:{strategy_config_sha256:strategyHash}});
  fs.writeFileSync(path.join(dir,"strategy.json"),JSON.stringify({payload:body,signature:createHmac("sha256",process.env.PRAMANA_REVIEW_SECRET!).update(body).digest("hex")}));
}

test("strategy review needs fresh engine evidence even when environment and attestation hashes agree", async () => {
  const { reviewedGate } = await import("../lib/review");
  sign();
  assert(reviewedGate("strategy",fresh()).pass);
  assert(!reviewedGate("strategy",{status:"running",mode:"paper"}).pass);
  sign("c".repeat(64));
  assert(!reviewedGate("strategy",fresh()).pass);
  sign();
});

test("changed, incomplete, stale, future and wrong-source runtime bindings never pass", async () => {
  const { verifiedRuntimeManifest } = await import("../lib/review");
  assert(verifiedRuntimeManifest(fresh()).pass);
  const mutations: Array<(r:Runtime)=>void> = [
    r => {r.status="stale";},
    r => {r.updatedAt=new Date(Date.now()-20000).toISOString();},
    r => {r.strategyManifest!.status="changed";},
    r => {r.strategyManifest!.status="incomplete";},
    r => {r.strategyManifest!.issues=["unsupported_component:custom"];},
    r => {r.strategyManifest!.sourceCheckAgeSeconds=66;},
    r => {r.strategyManifest!.checkedAt=new Date(Date.now()-20000).toISOString();},
    r => {r.strategyManifest!.checkedAt=new Date(Date.now()+20000).toISOString();},
    r => {r.strategyManifest!.bootSha256="d".repeat(64);},
    r => {r.strategyManifest!.sourceSha256="e".repeat(64);},
    r => {r.strategyManifest!.releaseRevision="f".repeat(40);},
  ];
  for (const mutate of mutations) {const r=fresh(); mutate(r); assert(!verifiedRuntimeManifest(r).pass,mutate.toString());}
});

test("absent, tampered and cross-tenant manifest registry rows fail closed", async () => {
  const { verifiedRuntimeManifest } = await import("../lib/review");
  db.prepare("UPDATE pilot_strategy_manifests SET payload='{}'").run();
  assert(!verifiedRuntimeManifest(fresh()).pass);
  db.prepare("UPDATE pilot_strategy_manifests SET payload=?,tenant_id='other'").run(payload);
  assert(!verifiedRuntimeManifest(fresh()).pass);
  db.exec("DELETE FROM pilot_strategy_manifests");
  assert(!verifiedRuntimeManifest(fresh()).pass);
});
