# Merge queue triage — 18 September 2026

`main` has not moved since 17 September (`bf38120`). Twenty-five branches carry unmerged
work under `src/` or `tests/`, and the commit titles across them read "Refresh X onto
owner-merged main", which is the signature of a queue nobody is draining: every branch keeps
re-basing onto a `main` that never advances.

**The queue looks like 25 problems. It is 12.**

## What the raw numbers say, and why they are wrong

Diffing each branch against `main`'s tip suggests near-total entanglement: 320 colliding pairs
out of 351, with 25 of 27 branches all touching `anthropic_client.py` and the same consensus
tests. That reading is an artifact. These branch tips are merge commits — "Merge commit …
into integration-147" — so a diff against `main`'s tip includes everything those merges
dragged in from `main` and from each other.

Measured from each branch's real merge-base instead, the picture changes: **160 colliding
pairs of 300**, and the files genuinely contested are a much smaller set.

| branches | file |
| ---: | --- |
| 14 | `src/quant_ai/agents/swarm_runtime.py` |
| 14 | `src/quant_ai/execution/daemon.py` |
| 12 | `src/quant_ai/execution/institutional.py` |
| 12 | `src/quant_ai/execution/paper_ledger.py` |
| 12 | `src/quant_ai/execution/program.py` |
| 12 | `src/quant_ai/execution/risk_authority.py` |
| 12 | `src/quant_ai/operations/institutional_recovery.py` |

## Most of the queue is two stacks

Thirteen of the twenty-five branches already contain other open branches as ancestors. They
are not competing — they are chains, and merging the tip merges the whole chain.

**Recovery / institutional stack — 10 branches, one merge.** Merging
`feat/position-linked-risk-release` subsumes `shared-paper-risk-reservations`,
`persisted-institutional-context`, `institutional-swarm-bridge`,
`authenticated-institutional-recovery`, `operator-audit-recovery-bundle`,
`persistent-operator-credentials`, `operator-recovery-console`, `recovery-only-api` and
`cohosted-recovery-lifecycle`.

**Learning / shadow stack — 5 branches, one merge.** Merging
`feat/receipt-timed-feature-ingestion` subsumes `learning-feedback-evidence`,
`shadow-forecast-lineage`, `offline-shadow-logistic-fitting` and
`point-in-time-training-dataset`.

That leaves **12 merge units**, and between those units only **14 colliding pairs of 66**.

## Drain order

Ordering barely matters — least-entangled-first and biggest-first both cost 14 forced
rebases. The win is recognising the stacks, not sequencing them. This order front-loads the
cheap ones so the queue visibly shortens early:

| # | unit | files | commits | forces |
| ---: | --- | ---: | ---: | ---: |
| 1 | `fix/headline-completion-evidence` | 3 | 2 | 0 |
| 2 | `feat/research-evaluation-lab` | 4 | 3 | 1 |
| 3 | `feat/research-provider-portfolios` | 6 | 2 | 0 |
| 4 | `fix/intelligence-input-verification` | 6 | 6 | 1 |
| 5 | `feat/post-126-paper-runtime` | 6 | 8 | 2 |
| 6 | `feat/widen-nse-watchlist` | 6 | 5 | 2 |
| 7 | `feat/receipt-timed-feature-ingestion` **(stack of 5)** | 29 | 22 | 2 |
| 8 | `fix/india-paper-readiness` | 6 | 3 | 2 |
| 9 | `feat/pilot-admits-mcx` | 14 | 8 | 2 |
| 10 | `fix/core-safety-closure-20260914` | 16 | 17 | 1 |
| 11 | `ci/source-snapshot-47e2d5c-20260918` | 21 | 28 | 1 |
| 12 | `feat/position-linked-risk-release` **(stack of 10)** | 49 | 43 | 0 |

The first six are 31 files and 26 commits between them. They can go today and they cut the
queue in half.

## Two cautions

**A stack tip is a large review, not a small one.** `position-linked-risk-release` is 49
files and 43 commits of other people's work arriving at once. Merging the tip is cheap in
rebases and expensive in review, and the review is the part that matters — the alternative is
merging its 10 members in order, which costs 10 reviews but no more total reading.

**These counts are structure, not correctness.** Nothing here says a branch is ready, only
what it would cost to land. CI still has to pass on each, and the nine jobs in `ci.yml` are
the arbiter of readiness.

## Reproducing this

Measure from merge-bases, never from `main`'s tip, or the merge commits will make everything
look like it collides with everything:

```bash
git fetch origin --depth=400 '+refs/heads/*:refs/remotes/origin/*'
git merge-base origin/main origin/<branch>          # the real base
git diff --name-only <base> origin/<branch> -- src tests
git merge-base --is-ancestor origin/<a> origin/<b>  # is <a> already inside <b>?
```
