# Item 2: declared sector-map mount in isolated container verification

## Reproduced failure

On head `fc78ef807151931ab538dd9101a2070d3bd673d2`, CI run 35188422874's
container job 105095401025 built both engine and UI images successfully. The engine
smoke fixture then failed with FileNotFoundError for `/app/pilot-sector-map.json`.

The verifier uses rendered Compose environment settings but intentionally constructs
its own disposable `docker run` commands. It carried the directives mount, not the
new sector-map mount. This is a CI fixture wiring defect, not evidence that the
runtime should accept a missing map or that either Dockerfile failed to build.

## Correction

The verifier now locates the bind whose target is the rendered service's
`PRAMANA_SECTOR_MAP_FILE`. It requires one read-only bind backed by an existing
absolute regular-file path, then carries that exact source/target into the isolated
engine's Docker invocation. Missing, ambiguous, writable or non-file inputs refuse.
No fallback to an empty map, hidden setting override or runtime-check exemption was
added. The runtime source under `src` and the previous 39 risk tests are unchanged.

The fixture remains the existing offline protection/telemetry smoke scenario, not
an authenticated real-data pilot or proof that required book-risk inputs have been
accepted on Lightsail. The container's external network remains disabled.

## Executed local evidence

Parent full Mac suite: 1575 passed and 13 existing deployment-portability failures.
Corrected suite: 1584 passed and the identical 13 failures. Both have 2 warnings
and 14 passing subtests. Exact JUnit failure identities match. No skips or relaxed
assertions. Nine new mount-contract cases pass; the combined focused set is
109 passed. Ruff and whitespace checks pass. All 454 selected source/test/script/
config hashes stayed unchanged during the full run.

Three fixture-specific mutations were run in disposable copies with no Docker
execution and no changes to the working source:

| Removed fixture protection | Regression that failed |
| --- | --- |
| Read-only declared bind requirement | `test_incomplete_or_unsafe_fixture_mount_refuses[writable]` |
| Existing source-file requirement | `test_a_rendered_path_is_not_proof_the_bind_source_exists[absent]` |
| Read-only Docker mount argument | `test_mount_binds_the_file_declared_by_the_actual_service` |

All three failed by assertions, with zero collection errors. These are only the
container fixture guards: they do NOT complete the separate, previously blocked
trading-risk source-guard-removal campaign. Exact evidence is committed in
`docs/evidence/pilot-container-map-mount.json`.

## Still draft

Require the corrected published-head CI result, successful real public/qualified
history, completed trading-risk guard-removal certification and eventual owner
host acceptance. Earlier five-green/one-red CI is retained, not overwritten as a
success. The two real history attempts remain dataReady=false with zero records.
No real login, host deployment, broker request, production secret read, service
restart, watchlist expansion, risk-cap increase or live-money setting change occurred.
