import fs from "node:fs";
import {createHash} from "node:crypto";
import {DatabaseSync} from "node:sqlite";

const feed = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml";
const hosts = new Set(["www.nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com"]);
export const nseSymbol = (value: unknown): value is string => typeof value === "string" && /^NSE:[A-Z0-9][A-Z0-9&._-]{0,35}$/.test(value);
export type EventMapping = {title: string; symbol: string; verifiedAt: string; provenance: string};
export type EventRevision = {id: string; title: string; description: string; sourceUrl: string; publishedAt: string; firstSeenAt: string; captureKind: "imported" | "direct_https"};
export type CompanyEvent = EventRevision & {guid: string; ambiguous: boolean; mapping: EventMapping | null; availableAt: string | null; history: EventRevision[]};
export type EventCapture = {observedAt: string; status: "ok" | "error"; kind: "imported" | "direct_https"; accepted: number | null; rejected: number | null; errorType: string | null; rawSha256: string | null};
export type CompanyEventsState = {status: "available" | "unavailable" | "invalid"; detail: string; asOf: string; evidenceSha256?: string; events: CompanyEvent[]; mappings: EventMapping[]; captures: EventCapture[]; revisionCount: number; excludedFutureRevisions: number; latestCapture: EventCapture | null; lastSuccess: EventCapture | null; captureStale: boolean; coverage: string};
const coverage = "NSE announcement RSS only. Imported captures and company disclosures are unverified; this is not a corporate-action adjustment engine or complete fundamental dataset.";
const hash = (value: string | Uint8Array) => createHash("sha256").update(value).digest("hex");
function check(value: unknown): asserts value {if (!value) throw new Error("Invalid company-event evidence");}
function text(value: unknown, max = 1000): value is string {return typeof value === "string" && value.trim().length > 0 && value.length <= max;}
function date(value: unknown): value is string {return typeof value === "string" && /(Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));}
function digest(value: unknown): value is string {return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);}
function integer(value: unknown): boolean {return Number.isSafeInteger(value) && Number(value) >= 0;}
// Matches Python canonical() for the string-only revision body.
function canonicalStrings(value: Record<string, string>): string {
  return JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0))).replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
export function officialEventUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {const url = new URL(value); return url.protocol === "https:" && hosts.has(url.hostname) && !url.username && !url.password && (!url.port || url.port === "443");} catch {return false;}
}
function open(write = false) {
  const file = process.env.PRAMANA_COMPANY_EVENTS_DB;
  check(file);
  const stat = fs.lstatSync(/* turbopackIgnore: true */ file);
  check(stat.isFile() && !stat.isSymbolicLink() && stat.size <= 50_000_000);
  const db = new DatabaseSync(file, {readOnly: !write});
  try {
    db.exec(`PRAGMA busy_timeout=2000;${write ? "" : "PRAGMA query_only=ON;"}`);
    return db;
  } catch (error) {db.close(); throw error;}
}
function read(db: DatabaseSync, at: string): CompanyEventsState {
  const cutoff = Date.parse(at); check(date(at) && cutoff <= Date.now() + 1000);
  const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all().map(r => r.name);
  check(JSON.stringify(tables) === JSON.stringify(["event_revisions", "feed_captures", "symbol_mappings"]));
  for (const table of tables) check(Number(db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get()?.n) <= 5000);
  // Include committed WAL content in the memory bound before loading any raw BLOBs.
  const bytes = db.prepare(`SELECT
    (SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM event_revisions) +
    (SELECT COALESCE(SUM(length(CAST(body AS BLOB)) + COALESCE(length(raw),0)),0) FROM feed_captures) +
    (SELECT COALESCE(SUM(length(CAST(title || verified_at || symbol || provenance AS BLOB))),0) FROM symbol_mappings) AS total`).get()?.total;
  check(typeof bytes === "number" && bytes <= 50_000_000);

  const rows = db.prepare("SELECT digest,body FROM event_revisions ORDER BY digest").all();
  const mappings = db.prepare("SELECT title,verified_at,symbol,provenance FROM symbol_mappings ORDER BY title,verified_at").all().map(row => {
    check(text(row.title) && date(row.verified_at) && nseSymbol(row.symbol) && text(row.provenance, 2000));
    return {title: row.title, symbol: row.symbol, verifiedAt: row.verified_at, provenance: row.provenance};
  });
  const captureRows = db.prepare("SELECT id,body,raw FROM feed_captures ORDER BY id").all();
  const captures = captureRows.map(row => {
    check(typeof row.body === "string" && row.body.length <= 1_000_000);
    const body = JSON.parse(row.body);
    check(body.feed_url === feed && date(body.observed_at) && ["imported", "direct_https"].includes(body.capture_kind));
    check(["ok", "error"].includes(body.status));
    if (body.status === "ok") {
      check(row.raw instanceof Uint8Array && row.raw.byteLength <= 5_000_000 && hash(row.raw) === body.raw_sha256);
      check(hash(body.raw_sha256 + body.observed_at) === row.id && integer(body.accepted) && Array.isArray(body.rejected));
    } else check(row.raw === null && hash(row.body) === row.id && text(body.error_type, 100));
    return {observedAt: body.observed_at, status: body.status, kind: body.capture_kind, accepted: body.status === "ok" ? body.accepted : null,
      rejected: body.status === "ok" ? body.rejected.length : null, errorType: body.status === "error" ? body.error_type : null, rawSha256: body.raw_sha256 ?? null} as EventCapture;
  });
  const grouped = new Map<string, EventRevision[]>(); let future = 0;
  for (const row of rows) {
    check(typeof row.body === "string" && row.body.length <= 100_000);
    const e = JSON.parse(row.body);
    check(text(e.title) && typeof e.description === "string" && e.description.length <= 30000 && text(e.guid, 4000) && officialEventUrl(e.source_url));
    check(date(e.published_at) && date(e.first_seen_at) && Date.parse(e.published_at) <= Date.parse(e.first_seen_at));
    check(digest(e.revision_sha256) && e.revision_sha256 === row.digest && e.feed_url === feed && ["imported", "direct_https"].includes(e.capture_kind));
    const original = {title: e.title, source_url: e.source_url, published_at: e.published_at, description: e.description, guid: e.guid};
    check(hash(canonicalStrings(original)) === e.revision_sha256);
    check(captures.some(c => c.status === "ok" && c.observedAt === e.first_seen_at && c.kind === e.capture_kind));
    if (Date.parse(e.first_seen_at) > cutoff || Date.parse(e.published_at) > cutoff) {future++; continue;}
    const revision: EventRevision = {id: e.revision_sha256, title: e.title, description: e.description, sourceUrl: e.source_url, publishedAt: e.published_at, firstSeenAt: e.first_seen_at, captureKind: e.capture_kind};
    grouped.set(e.guid, [...(grouped.get(e.guid) || []), revision]);
  }
  const effective = new Map<string, EventMapping>();
  for (const mapping of mappings) if (Date.parse(mapping.verifiedAt) <= cutoff && Date.parse(mapping.verifiedAt) > Date.parse(effective.get(mapping.title)?.verifiedAt || "1970-01-01")) effective.set(mapping.title, mapping);
  const events = [...grouped.entries()].map(([guid, versions]) => {
    const history = versions.sort((a, b) => Date.parse(b.firstSeenAt) - Date.parse(a.firstSeenAt) || a.id.localeCompare(b.id));
    const latest = history[0], ambiguous = history.length > 1 && Date.parse(latest.firstSeenAt) === Date.parse(history[1].firstSeenAt);
    const mapping = effective.get(latest.title) || null;
    return {...latest, guid, ambiguous, mapping, availableAt: mapping && !ambiguous ? new Date(Math.max(Date.parse(mapping.verifiedAt), Date.parse(latest.firstSeenAt))).toISOString() : null, history};
  }).sort((a, b) => Date.parse(b.firstSeenAt) - Date.parse(a.firstSeenAt) || a.id.localeCompare(b.id));
  const knownCaptures = captures.filter(c => Date.parse(c.observedAt) <= cutoff).sort((a, b) => Date.parse(b.observedAt) - Date.parse(a.observedAt));
  const latestCapture = knownCaptures[0] || null, lastSuccess = knownCaptures.find(c => c.status === "ok") || null;
  const state: CompanyEventsState = {status: "available", detail: "Recorded disclosure evidence; source coverage and economic content remain unverified.", asOf: at,
    evidenceSha256: hash(JSON.stringify({rows, mappings, captures})), events, mappings: mappings.filter(m => Date.parse(m.verifiedAt) <= cutoff), captures: knownCaptures,
    revisionCount: rows.length - future, excludedFutureRevisions: future, latestCapture, lastSuccess,
    captureStale: !lastSuccess || cutoff - Date.parse(lastSuccess.observedAt) > 86400000, coverage};
  check(Buffer.byteLength(JSON.stringify(state)) <= 4_000_000);
  return state;
}
export function readCompanyEvents(at = new Date().toISOString()): CompanyEventsState {
  const empty = {asOf: at, events: [], mappings: [], captures: [], revisionCount: 0, excludedFutureRevisions: 0, latestCapture: null, lastSuccess: null, captureStale: true, coverage};
  if (!process.env.PRAMANA_COMPANY_EVENTS_DB) return {...empty, status: "unavailable", detail: "Company-event storage has not been configured."};
  let db: DatabaseSync | undefined;
  try {db = open(); db.exec("BEGIN"); return read(db, at);} catch {return {...empty, status: "invalid", detail: "Company-event evidence is missing, invalid or exceeds the workspace limit. Other panels remain available."};} finally {db?.close();}
}
export function saveCompanyMapping(body: unknown, allowedSymbols: string[]) {
  check(body && typeof body === "object"); const b = body as Record<string, unknown>;
  check(Object.keys(b).sort().join() === ["expectedVerifiedAt", "provenance", "reviewConfirmed", "symbol", "title"].sort().join());
  check(text(b.title) && nseSymbol(b.symbol) && allowedSymbols.includes(b.symbol) && text(b.provenance, 2000) && b.reviewConfirmed === true);
  check(b.expectedVerifiedAt === null || date(b.expectedVerifiedAt));
  const db = open(true);
  try {
    db.exec("BEGIN IMMEDIATE"); const at = new Date().toISOString(); const current = read(db, at);
    check(current.events.some(e => e.title === b.title));
    const history = current.mappings.filter(m => m.title === b.title).sort((a, b) => Date.parse(b.verifiedAt) - Date.parse(a.verifiedAt));
    if ((history[0]?.verifiedAt ?? null) !== b.expectedVerifiedAt) throw new Error("Mapping changed. Refresh and review the latest record.");
    // Do not let an earlier browser request overwrite a later/backdated CLI record.
    const all = db.prepare("SELECT verified_at FROM symbol_mappings WHERE title=?").all(b.title);
    check(all.every(r => Date.parse(String(r.verified_at)) < Date.parse(at)));
    db.prepare("INSERT INTO symbol_mappings(title,verified_at,symbol,provenance) VALUES (?,?,?,?)").run(b.title, at, b.symbol, b.provenance.trim());
    db.exec("COMMIT"); return {title: b.title, symbol: b.symbol, verifiedAt: at, provenance: b.provenance.trim()};
  } finally {db.close();}
}
export function companyEventsContext(at?: string) {
  const state = readCompanyEvents(at);
  const eligible = state.events.filter(e => e.mapping && !e.ambiguous);
  return {status: state.status, detail: state.detail, asOf: state.asOf, evidenceSha256: state.evidenceSha256,
    coverage: state.coverage, captureStale: state.captureStale, latestCapture: state.latestCapture,
    totalEvents: state.events.length, unmappedOrAmbiguous: state.events.length - eligible.length, includedEvents: Math.min(eligible.length, 30),
    events: eligible.slice(0, 30).map(e => ({id: e.id, title: e.title.slice(0, 200), titleTruncated: e.title.length > 200, description: e.description.slice(0, 1500), descriptionTruncated: e.description.length > 1500,
      symbol: e.mapping!.symbol, sourceUrl: e.sourceUrl.length <= 800 ? e.sourceUrl : null, sourceUrlOmitted: e.sourceUrl.length > 800, publishedAt: e.publishedAt, firstSeenAt: e.firstSeenAt, availableAt: e.availableAt, captureKind: e.captureKind}))};
}
