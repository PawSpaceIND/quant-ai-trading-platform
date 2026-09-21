"use client";
import {useMemo, useState} from "react";
import {companyEventQuestion} from "../lib/company-event-prompt";
import {watchlisted} from "../lib/symbols";
import type {CompanyEvent, CompanyEventsState, EventMapping} from "../lib/company-events";
const when = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "Unavailable";
async function request(url: string, options?: RequestInit) {
  const response = await fetch(url, {cache: "no-store", signal: AbortSignal.timeout(10000), ...options});
  if (response.status === 401) {window.location.assign("/login"); throw new Error("Session expired");}
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || body.detail || "Company-event request failed");
  return body;
}
function MappingEditor({event, symbols, history, refresh, onNotice}: {event: CompanyEvent; symbols: string[]; history: EventMapping[]; refresh: () => Promise<void>; onNotice: (message: string) => void}) {
  const [action, setAction] = useState<"map" | "revoke">("map");
  const [symbol, setSymbol] = useState(event.mapping?.symbol || "");
  const [reference, setReference] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); onNotice("");
    try {
      const result = await request("/api/company-events", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action, title: event.title, symbol: action === "revoke" ? "" : symbol, provenance: reference, reviewConfirmed: confirmed, expectedVerifiedAt: event.latestMapping?.verifiedAt ?? null})});
      onNotice(`${event.title}: ${result.mapping.status === "revoked" ? "Mapping withdrawn" : "Mapping recorded"} at ${when(result.mapping.verifiedAt)}. Earlier records remain in history.`); setReference(""); setConfirmed(false);
    } catch (error) {onNotice(`${error instanceof Error ? error.message : "Could not confirm saving."} Refresh and inspect history before retrying.`);}
    finally {await refresh(); setBusy(false);}
  }
  return <details className="event-mapping"><summary>Review company-to-symbol mapping</summary>
    <p>Record an exact company match after checking an instrument reference. This adds a dated operator assertion; it does not certify the company or change a trading watchlist.</p>
    <form onSubmit={save}>
      <label>Review action<select aria-label="Company mapping action" value={action} onChange={e => {setAction(e.target.value as "map" | "revoke"); setConfirmed(false); setReference("");}} disabled={busy}><option value="map">Record a symbol mapping</option><option value="revoke" disabled={!event.latestMapping || event.latestMapping.status === "revoked" && !event.mappingAmbiguous}>Withdraw the current mapping</option></select></label>
      {action === "map" && <label>NSE instrument<select aria-label="Event mapping instrument" required value={symbol} onChange={e => setSymbol(e.target.value)} disabled={busy}><option value="">Select an observed NSE instrument</option>{symbols.map(s => <option key={s}>{s}</option>)}</select></label>}
      <label>{action === "revoke" ? "Withdrawal reason and reviewed reference" : "Reviewed instrument reference"}<textarea aria-label="Reviewed instrument reference" required maxLength={1000} value={reference} onChange={e => setReference(e.target.value)} disabled={busy} placeholder={action === "revoke" ? "Explain why this company-to-symbol mapping must be withdrawn." : "Record the source and exact company/instrument match you checked."} /></label>
      <label className="event-confirm"><input type="checkbox" required checked={confirmed} onChange={e => setConfirmed(e.target.checked)} disabled={busy} /> {action === "revoke" ? "I reviewed this withdrawal; later automatic company context must exclude this mapping until another review." : "I checked this exact company against that instrument reference."}</label>
      <button disabled={busy || !confirmed || !reference.trim() || action === "map" && (!symbols.length || !symbol)}>{busy ? "Saving review…" : action === "revoke" ? "Withdraw mapping" : "Record mapping"}</button>
      {action === "map" && !symbols.length && <p className="muted">No NSE instruments are available from the configured market list.</p>}
    </form>
    <h4>Mapping history</h4>{history.length ? <ol>{[...history].reverse().map(m => <li key={m.verifiedAt}><strong>{m.status === "revoked" ? "Mapping withdrawn" : m.symbol}</strong> · {when(m.verifiedAt)}<p>{m.provenance}</p></li>)}</ol> : <p>No mapping recorded.</p>}
  </details>;
}
export function CompanyEventsPanel({state, symbols, favorites, onAsk, onRefresh}: {state?: CompanyEventsState; symbols: string[]; favorites: string[]; onAsk: (question: string, asOf?: string) => void; onRefresh: () => Promise<void>}) {
  const [reviewNotice, setReviewNotice] = useState("");
  const [search, setSearch] = useState(""); const [savedOnly, setSavedOnly] = useState(false);
  const [requestedPage, setPage] = useState(0); const [selected, setSelected] = useState("");
  const [at, setAt] = useState(""); const [historical, setHistorical] = useState<CompanyEventsState | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const data = historical || state;
  const filtered = useMemo(() => (data?.events || []).filter(e => (!savedOnly || (!!e.mapping && !e.ambiguous && watchlisted(e.mapping.symbol, favorites))) && `${e.title} ${e.mapping?.symbol || ""} ${e.description}`.toLowerCase().includes(search.toLowerCase())), [data, savedOnly, favorites, search]);
  const page = Math.min(requestedPage, Math.max(0, Math.ceil(filtered.length / 10) - 1));
  const event = filtered.find(e => e.id === selected) || filtered[page * 10];
  async function loadTime() {
    setBusy(true); setError(""); setReviewNotice("");
    try {const instant = new Date(at).toISOString(); setHistorical(await request(`/api/company-events?at=${encodeURIComponent(instant)}`)); setPage(0); setSelected("");}
    catch (e) {setHistorical(null); setError(e instanceof RangeError ? "That cutoff could not be read. Enter a full date and time." : e instanceof Error ? e.message : "Evidence could not be loaded.");}
    finally {setBusy(false);}
  }
  return <section className="panel company-events-panel">
    <div className="panel-title"><div><span className="eyebrow">DATED SOURCE EVIDENCE</span><h2>Company announcements</h2></div><span className="muted">Research only</span></div>
    {!data || data.status !== "available" ? <p className="empty">{data?.detail || "Company-event storage has not been configured."}</p> : <>
      <p className="research-notice">{data.coverage}</p>
      <dl className="research-stats"><div><dt>Last recorded attempt</dt><dd>{when(data.latestCapture?.observedAt)}</dd></div><div><dt>Attempt status</dt><dd>{data.latestCapture ? `${data.latestCapture.status} · ${data.latestCapture.kind.replaceAll("_", " ")}` : "No capture"}</dd></div><div><dt>Last successful capture</dt><dd>{when(data.lastSuccess?.observedAt)}</dd></div><div><dt>Available records / revisions</dt><dd>{data.events.length} / {data.revisionCount}</dd></div></dl>
      {data.latestCapture?.status === "error" && <p className="research-notice" role="status">Latest source attempt failed: {data.latestCapture.errorType}. Retained announcements may be outdated.</p>}
      {data.captureStale && <p className="research-notice">No successful capture within 24 hours of this view’s cutoff. This age threshold does not establish full market-session coverage.</p>}
      <p className="muted">Known by {when(data.asOf)} · {data.excludedFutureRevisions} later revisions excluded. Refresh reads stored evidence; it does not fetch the exchange feed.</p>
      <div className="event-controls"><label>Search company or symbol<input aria-label="Search company announcements" value={search} onChange={e => {setSearch(e.target.value); setPage(0);}} /></label><label className="event-confirm"><input type="checkbox" checked={savedOnly} onChange={e => {setSavedOnly(e.target.checked); setPage(0);}} /> Watchlist only</label></div>
      <div className="event-controls"><label>Known at<input aria-label="Company evidence cutoff" type="datetime-local" step="1" value={at} onChange={e => setAt(e.target.value)} /></label><button disabled={busy || !at} onClick={() => void loadTime()}>{busy ? "Loading…" : "Apply time"}</button><button onClick={() => {setHistorical(null); setAt(""); setError(""); setReviewNotice(""); void onRefresh();}}>Current evidence</button><a href={`/api/company-events?download=1&at=${encodeURIComponent(data.asOf)}`} download>Export event evidence ↓</a></div>
      {error && <p role="alert">{error}</p>}
      {reviewNotice && <p role="status" className="research-notice">{reviewNotice}</p>}
      <div className="event-workspace"><div><div className="event-list" aria-label="Company event results">{filtered.slice(page * 10, page * 10 + 10).map(e => <button key={e.id} aria-pressed={e.id === event?.id} onClick={() => setSelected(e.id)}><strong>{e.title}</strong><span>{e.mapping?.symbol || (e.mappingAmbiguous ? "Mapping conflict" : e.latestMapping?.status === "revoked" ? "Mapping withdrawn" : "Unmapped")} · {e.ambiguous ? "Conflicting revisions" : e.captureKind === "imported" ? "Imported" : "Recorded HTTPS capture"}</span><span>First seen {when(e.firstSeenAt)}</span></button>)}</div>{!filtered.length && <p>No announcements match these filters.</p>}
        <div className="replay-pagination"><span>{filtered.length ? page * 10 + 1 : 0}–{Math.min(page * 10 + 10, filtered.length)} of {filtered.length}</span><button aria-label="Previous company events" disabled={page === 0} onClick={() => {setPage(page - 1); setSelected("");}}>Previous</button><button aria-label="Next company events" disabled={(page + 1) * 10 >= filtered.length} onClick={() => {setPage(page + 1); setSelected("");}}>Next</button></div>
      </div>{event && <article className="event-detail"><h3>{event.title}</h3><p>{event.mapping?.symbol || (event.mappingAmbiguous ? "Conflicting mapping reviews" : event.latestMapping?.status === "revoked" ? "Mapping withdrawn" : "No reviewed symbol mapping")}</p><p>{event.description || "No description supplied."}</p>
        <dl><dt>Published</dt><dd>{when(event.publishedAt)}</dd><dt>First seen</dt><dd>{when(event.firstSeenAt)}</dd><dt>Mapped evidence available from</dt><dd>{when(event.availableAt)}</dd></dl>
        {(event.ambiguous || !event.mapping) && <p className="research-notice">{event.ambiguous ? "Conflicting revisions share the same first-seen time." : event.mappingAmbiguous ? "Mapping reviews share the same recorded time." : event.latestMapping?.status === "revoked" ? "This mapping was withdrawn at or before the selected cutoff." : "This company has no mapping at this cutoff."} Excluded from automatic symbol-specific Atlas context.</p>}
        <a href={event.sourceUrl} target="_blank" rel="noopener noreferrer">Open recorded NSE source ↗</a><p className="footnote">Revision SHA-256: <code>{event.id}</code>. The local hash is not independent source certification.</p>
        <details><summary>Revision history ({event.history.length})</summary><ol>{event.history.map(r => <li key={r.id}><strong>{r.title}</strong> · First seen {when(r.firstSeenAt)}<p>{r.description}</p><span className="footnote">{r.id}</span></li>)}</ol></details>
        {historical ? <p className="muted">Return to current evidence to record a mapping. Historical views never backdate a review.</p> : <MappingEditor key={`${event.id}:${event.latestMapping?.verifiedAt || "unmapped"}:${event.mappingAmbiguous}`} event={event} symbols={symbols} history={data.mappings.filter(m => m.title === event.title)} refresh={onRefresh} onNotice={setReviewNotice} />}
        <button className="event-ask" onClick={() => onAsk(companyEventQuestion(event, data.asOf), data.asOf)}>Discuss announcement with Atlas ↗</button>
      </article>}</div>
      <details><summary>Capture history ({data.captures.length})</summary><ol>{data.captures.slice(0, 30).map((c, i) => <li key={`${c.observedAt}:${i}`}>{when(c.observedAt)} · {c.kind} · {c.status}{c.status === "ok" ? ` · ${c.accepted} accepted / ${c.rejected} rejected items` : ` · ${c.errorType}`}</li>)}</ol>{data.captures.length > 30 && <p>The latest 30 captures are shown; the export includes all captures within the workspace limit.</p>}</details>
    </>}
  </section>;
}
