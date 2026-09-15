# Per-decision model, request and input provenance

New Atlas decisions now carry structured provenance through the proposal and XAI trace into the atomic governed paper-fill record. The previous rationale string named a configured model, but did not retain the exact request or provider-reported identity.

## Recorded evidence

`pramana.decision_provenance.v1` records the Atlas/founder policy values and full founder guidance, declared specialist evidence, observation time and supplied market tick. Canonical JSON SHA-256 fingerprints identify these configuration and input snapshots. The mode distinguishes deterministic decisions, completed model calls, unavailable calls, invalid schemas and unverified custom inference. Deterministic decisions do not claim a model call.

Each normal Anthropic adapter result carries a separate `pramana.inference.v1` record containing:

- The requested model, provider-reported model and response ID when returned. An absent returned identity remains null; it is not replaced with the requested alias in the structured record.
- Exact SDK request arguments: system instruction, user prompt, tool schema/choice and token limit, with request/prompt/system/tool fingerprints. API credentials are not part of these arguments.
- Adapter transport kind, installed SDK version, timeout setting, timestamps and elapsed time.
- Reported input/output/cache token counts when available, with missing counts null rather than zero.
- Completion, unavailability or invalid-schema status. Successful tool payloads also have a content fingerprint.

The metadata belongs to the returned call result rather than a shared last-response field, so overlapping requests cannot exchange identities. The strict model tool schema is unchanged. Invalid responses and timeouts preserve attempt metadata on the resulting neutral decision. Filled traces persist through the existing atomic ledger transaction; rejected decisions retain the existing logger's persistence behavior.

## UI and privacy

Activity Inspect and Overview show the decision source and requested/returned model identities. Test/custom SDK transports are explicitly labeled. Activity also shows request and Atlas-configuration fingerprints. Legacy proofs remain labeled unrecorded; they are not retroactively assigned today's model or prompt.

Dashboard responses expose a bounded metadata summary, not the full request and input snapshots. Full prompts and founder guidance reside in the private proof/ledger stores and their backups and need the same access/retention protections as that evidence. Fingerprints identify content; they do not encrypt or authenticate it.

The proof detail panel is constrained to the visible journal width so long model names and fingerprints wrap on phones. Journal columns retain horizontal scrolling; opening an inspector resets that horizontal position to reveal the detail.

## Verification and limits

All **366 Python tests** and **18 dashboard tests** pass, along with Ruff, TypeScript and the local webpack production build. New tests cover exact request hashing, differing requested/returned identity, overlapping calls completing in reverse order, timeout and invalid-schema metadata, configuration changes, missing/invalid token metadata and propagation into a committed fill.

An isolated mocked-SDK drill saved the provenance with a paper BUY. Recomputed request, configuration and input hashes matched, and accounting reconciled. Desktop Activity and Overview displayed the identities. At a 390-pixel viewport, document width was 390 pixels and the expanded detail was 308 pixels wide, ending at x=349. Browser warning/error logs were empty. No real inference or broker order was sent in this drill.

This advances traceability, not statistical qualification. A provider-reported model name does not guarantee immutable weights or deterministic replay. The Atlas configuration fingerprint is **not** the complete strategy-configuration hash used by the release-review gate. Complete runtime/code/data version binding, raw licensed-data provenance, strategy-level outcome attribution, drift/calibration monitoring and AI-specific holdout/forward evidence remain open requirements. No existing acceptance was signed or upgraded by adding these fields.
