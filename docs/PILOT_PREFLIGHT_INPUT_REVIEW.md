# Item 2 preflight input/report review — OPEN acceptance findings

## Scope and actual outcome

Reviewed committed head: `ba360107db8fd61a0d4d157daf796cf175b2182e` of PR #133.
This continuation adds executable acceptance evidence and documentation only.
The attempted repair to `scripts/check_pilot_risk_gates.py` was tool-blocked before
execution. It was not retried through another tool or rewritten into this commit.
Every previously tracked file, including the preflight and trading runtime, remains
unchanged. The separate active container-smoke worktree is untouched.

**These are reproduced/open requirements, not completed fixes. Item 2 remains draft.**
The ordinary suite is not used to imply that these new acceptance cases pass.

## Executed tests

- Fresh full baseline: **1584 passed, 13 failed**, 2 warnings and 14 subtests passed.
- New explicit input/report acceptance: **20 failed, 2 passed**, zero collection
  errors and exit 1. Repeated after moving the evidence to its retained path with
  exactly the same result.
- No real history fetch, broker request, model call or production credential read.
- No guard-removal campaign was completed. These are synthetic boundary cases,
  not a substitute for the source-guard-removal certification required by the brief.

Run the unfinished acceptance explicitly:

```bash
PYTHONPATH=src python -m pytest -q acceptance/test_risk_preflight_input_boundary.py
```

The new file is intentionally outside the existing `testpaths = ["tests"]` default
collection. No existing test is moved, skipped, marked xfail or weakened. Its red
result is recorded here and in the PR; a green ordinary CI job cannot close it.
The tests substitute the expensive readiness check with controlled return values
so they exercise CLI input validation, sequencing and report publication only.
They are NOT evidence of a live trading-risk bypass or successful market acceptance.

## Findings and proposed acceptance conditions

### Ambiguous input is accepted before history work

The current directives loader uses ordinary `json.loads`. Duplicate top-level and
nested keys retain their last value; a duplicate or escaped equivalent key is not
rejected. The CLI also lacks explicit rejection of non-JSON numeric constants and
an explicit top-level-object check. The sector loader already rejects exact duplicate
keys, but the two inputs do not share the same parsing contract.

Proposed acceptance: reject duplicate keys at every depth, non-JSON numbers and
non-object roots before provider work. The size-bound cases propose a 1 MiB maximum
for operator configuration input; that is a new engineering limit for review, NOT
an owner-specified threshold, exchange rule, margin figure or measured exploit.

Primary Python JSON behavior reference:
https://docs.python.org/3/library/json.html#standard-compliance-and-interoperability

### A result is not bound to the source files used

The current report contains a digest of observed history rows but does not bind the
exact directives and sector-map input bytes. Modifying either input while the check
runs is not detected. In the synthetic CLI test, a controlled successful readiness
result is still published after that input change.

Proposed acceptance: report the SHA-256 of each input snapshot and confirm unchanged
bytes at completion before publication. A byte comparison at two instants would not
prove continuous immutability against a writer changing and restoring a file between
those checks, nor would hashes authenticate the input's real-world truth.

### Destination and complete-report publication

The output file is opened only after provider work, so an existing destination,
directory, dangling link or missing parent is discovered too late. Exclusive open
already protects an existing/concurrently created report from overwrite; the two
passing cases confirm concurrency refusal and the explicit online-consent boundary.

Proposed acceptance: reject unavailable destinations before network work, and publish
only a complete private report. The current direct write provides no completed-file
atomic publication contract; the flush-failure case requires that boundary. This is
not a claim that a real production report was corrupted. A failure report with
`dataReady=false` should remain nonzero and never imply host acceptance.

## Evidence and unchanged release gates

`docs/evidence/pilot-preflight-input-review.json` records exact test names, the
reviewed source hash, retained test hash, counts and limitations. The 20 failing
cases are not 20 independent production incidents; some are proposed hardening
contracts such as private atomic report output and input size bounds.

Actual usable real history, source-guard-removal certification, running-host gate
readiness and owner review/merge/deployment remain separate open Item 2 gates.
This continuation did not retry the previous rate-limited provider, disable TLS,
change sector grouping, alter risk thresholds or create market bars. It does not
certify the three gates armed on Lightsail. Item 3 must remain blocked until the
owner merges Item 2. MCX admission and all live-money controls are untouched.

## Final ordinary-suite and CI checkpoint

Ordinary suite after retaining the new evidence: **13 failed, 1584 passed, 2 warnings, 14 subtests passed in 59.67s**.
The exact 13 failure identities match the fresh baseline, and all 454 existing
source/test/script/configuration hashes are unchanged. Ruff `src tests`, the new
acceptance file and the existing preflight script pass; whitespace checks pass.
The absolute project venv was used because `/usr/local/bin/ruff` remains absent.

Reviewed-base CI run 35189036516 completed with a failing container job
105097286575; test, UI, browser-UI, cloud-web and security jobs passed. This is not
new-head certification and does not include these unfinished acceptance cases.
The container-smoke work in the separate peer checkout was not changed here.
