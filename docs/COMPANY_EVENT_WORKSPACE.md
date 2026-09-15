# Company announcement workspace

The private Markets screen reads the existing NSE company-event database. Search, saved-watchlist filtering, pagination, disclosure details, source links, revision and capture history, historical cutoffs, mapping review/withdrawal, private export and Atlas handoff are connected. This is stored research evidence, not a live news terminal, fundamental-data service or corporate-action accounting engine.

## Configuration and collection

Set `PRAMANA_COMPANY_EVENTS_DB` to the absolute path of the existing database maintained by `scripts/research_extensions.py`. The UI never creates a missing store or fetches the feed. Refresh reloads stored evidence. Use the [existing collector commands](research-provider-portfolios.md#company-event-evidence) to collect or import announcements. A successful real target-host capture, source rights and instrument mappings still need qualification.

The supported store contains exactly `event_revisions`, `feed_captures` and `symbol_mappings`. Reads use a SQLite read transaction and query-only connection. The workspace rejects missing, malformed or oversized configured evidence while other panels remain available. Limits are 5,000 rows per table, a 50 MB source file and combined stored-content budget (including committed WAL content), and a 4 MB rendered report. These are pilot bounds; archival/retention and higher-volume indexing remain separate work.

This is a single-founder private workspace. The server selects the database; clients cannot supply a path. Do not share one company-event database between tenants with different access rights. The authenticated founder can inspect retained references and record mappings. Neither the shared session nor an operator reference is an independently certified reviewer identity.

## Dates, revisions and mappings

- A historical cutoff includes only revisions first observed by that time and mappings recorded by that time. Publication time alone cannot backdate availability.
- The latest known revision for a GUID supersedes older content before symbol filtering. A newer unmapped correction cannot revive the old mapped announcement. Equally recent conflicting revisions or mapping reviews are withheld from automatic symbol-specific context. Both readers preserve microsecond cutoff precision.
- Mapping review requires an exact known company title, an NSE symbol from the configured market list, a reference and an explicit checked assertion. Saving appends a server-timestamped record. Stale competing requests fail with 409; no history is overwritten or backdated. The Python research reader consumes the same rows.
- Historical views disable mapping changes. Correction to a different supported symbol and withdrawal are available through dated, append-only reviews. See the [mapping lifecycle](COMPANY_MAPPING_LIFECYCLE.md) for concurrency, precise cutoff and rollback rules.
- Capture failure and age remain visible. The 24-hour age threshold is a display convention, not proof of complete session coverage. Imported XML remains labelled imported.

Raw capture hashes and revision-content hashes are checked. Stored capture linkage is checked against observation time and capture kind. The reader does not independently reparse raw XML to certify economic facts. The report digest fingerprints the entire current stored evidence inventory; in a historical view it is an inventory identifier, not a historical model feature or external signature. Future revision counts are diagnostics only.

## Atlas and export

`GET /api/company-events?at=ISO_TIME&download=1` produces authenticated, no-store JSON. It excludes raw feed bytes and private capture-error messages; it includes retained mapping references. Missing configuration returns 404 and invalid configured evidence returns 503. Session, exact-origin mutation protection, payload limits and mapping rate limits apply through the private API.

Atlas handoff prefills a bounded, valid-JSON description with the selected record's ID, dates, source, symbol, import/ambiguity status and truncation flags. It does not automatically submit. The request carries a structured `companyAsOf` cutoff: the server supplies only company disclosure context available at that time and excludes current portfolio/market data and prior conversation. The exact context is persisted with the conversation. The panel names this scope; **Use full current workspace** explicitly returns to ordinary workspace context for subsequent requests.

Automatic company context includes at most 30 mapped, unambiguous records with titles bounded to 200 characters, descriptions to 1,500 characters and source URLs omitted above 800 characters with explicit flags; counts and coverage limitations remain visible. Manually selected unmapped/conflicting content retains its warning in the prompt. Model weights can still contain later knowledge, and user text remains user-supplied evidence: this mode is not a leakage-free historical AI backtest. Atlas has no execution or mapping tools. Private company evidence is removed by both the cloud publisher and Worker; the hosted viewer does not expose this workflow.

## Verification — 14 September 2026

- Combined suite: 503 Python, 40 UI and 13 Worker tests pass; CI-scope Ruff (`src tests`), TypeScript and webpack production build pass. An optional repository-wide Ruff scan found seven existing script findings outside CI's scope; they remain maintenance work.
- A deterministic Python-produced fixture verifies Python/Node eligibility at six cutoffs, Unicode hashes, correction suppression, equal-time conflicts, read-only behavior, mapping concurrency, unsafe links, corrupt captures, privacy and bounded persisted Atlas context.
- Synthetic browser fixture: 19 captures, 18 revisions and 15 current events. Search, 10/5 pagination, watchlist filtering, mapping save/history, historical cutoff, authenticated download and selected-record Atlas handoff pass. At 04:00:25Z on 1 January, only the corrected disclosure and two known revisions remain; 16 later revisions are excluded.
- Desktop and 390px mobile were inspected. Mobile document width is 390px, disclosure detail 324px and the final Atlas cutoff notice 330px. Full-current-context reset and mobile close work. No browser warnings/errors were captured.
- API checks: unauthenticated reads 401; wrong/missing mutation Origin 403; authenticated export 200; stale mapping retry 409; invalid symbol/oversized body 400; rate limit 429. Corrupt capture yields event 503 while workspace stays 200 with unchanged readiness checks; the fixture was restored.
- The actual browser-to-API request persisted cutoff `2026-01-01T04:00:25.000Z`, one corrected disclosure and no current portfolio/market context. With the provider key empty it saved and displayed the expected setup error. A separate mocked provider test verifies that earlier chat and later company content are absent from the outbound request.

All checks in this increment used isolated synthetic evidence. No external provider request, live source fetch, order, deployment or real acceptance was performed. Real source completeness, independently reviewed mappings, prospective provider outcomes and target-host operations remain open.


The subsequent [mapping lifecycle increment](COMPANY_MAPPING_LIFECYCLE.md) adds withdrawal/re-review, explicit withdrawn/conflicting state, persistent save feedback and microsecond eligibility. Its 507/43/13 regression and browser/API/CLI verification supersede the corresponding missing-work items above.
