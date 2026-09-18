# Item 3: read-only AI budget observation

The existing watchlist generator reports logical work opportunities, not measured
bills. `scripts/inspect_watchlist_budget.py` closes the missing operator-observation
interface: it reads the actual container environment's budget limits and the existing
`ai_budget` table without making model/provider calls or changing the budget.

It reuses the deployed `quant_ai.llm.budget` defaults, environment parser, database
name and the existing consensus/headline scope constants. It never constructs the
budget writer, renews credentials, starts a daemon or modifies `.env`.

## Invocation and authority

Run only as an explicitly reviewed diagnostic in the existing paper container.
This does not require deploying the watchlist branch. For a reviewed local copy:

```bash
docker exec -i deploy-pramana-ghost-1 python -B - --days 3 < inspect_watchlist_budget.py
```

The stdin path is covered by an executable subprocess test. The script must run with
the installed Pramana dependencies. It takes no credential arguments. Its default
path is `PRAMANA_AI_BUDGET_DB`, or `ai-budget.sqlite` next to the configured paper
ledger. An optional `--database` selects an existing file; a missing database is
refused rather than created. The read-only URI retains SQLite WAL visibility; no
`immutable=1` shortcut is used on the running database.

`--days` is one to seven UTC dates including the current UTC date. A single read
transaction obtains both allowed scopes consistently. Query-only mode, untrusted
schema mode, a progress limit and bounded returned rows are separate checks. Symlinks,
views masquerading as the budget table, duplicate rows, malformed dates/counters,
disabled/unusable limits and a non-paper setting refuse with fixed diagnostics.

The report prints selected numeric counters/limits only. It does not print database
paths, raw SQL errors, environment contents, access tokens, prompt text or arbitrary
scope names. Existing unrelated scopes are excluded rather than copied into output.
A present zero row is distinct from a missing row: missing values remain null.

## Interpretation

- Limits apply independently to consensus and headline_sentiment per UTC date.
- Calls are reserved logical calls, not successful completions, paid HTTP attempts,
  analyses or fills. A failed call can still consume a reservation.
- Recorded input/output tokens exclude separate cache-billing categories and may
  omit in-flight usage. The counters alone cannot establish a complete API bill.
- UTC budget dates must not be relabelled as IST trading sessions. At 00:15 IST,
  the budget is still on the previous UTC date.
- `belowBothCountersAtSnapshot` is a read-time comparison, not authority to reserve
  another call later and not approval of a fifty-name rollout.
- The number of complete fifty-name consensus sweeps allowed by the call count is
  an upper bound. Token exhaustion, sequential latency and other gates can reduce it.
- Monetary cost, forecast token demand, paid-request count and measured latency remain
  null. No provider price, statutory rate, exchange rate or new budget is invented.

The actual host report remains outstanding until the owner runs the diagnostic.
Synthetic test counters are never labelled as AWS usage. The official constituent
CSV, date/source qualification, fifty-name mappings and history/feed acceptance
remain separate blockers. Keep the branch draft, five-name AWS scope and paper mode.

## Executed verification

Isolated Python 3.13: **51 behavior cases and 22 paired mutation cases = 73 passed**,
zero failures/errors/skips in 115.04 seconds. Each mutation requires a passing control,
an assertion failure after the designated change, restoration and unchanged original
hashes. The inventory is `docs/evidence/nse-watchlist-budget-guard-inventory.json`.
Existing budget tests plus the new behavior tests: **61 passed**. Ruff passes via the
explicit isolated virtual-environment interpreter; `/usr/local/bin/ruff` is absent.

This local workspace recovered the prior tracked source archive at `8234354...`;
the imported budget modules are unchanged through target parent `4df8dec...` (none
are in that PR's changed-file list). Only this new surface and unchanged budget tests
were run locally; this is not a new complete 3,115-case local run. Fresh exact-head
GitHub CI remains necessary and its actual result is recorded on PR #144.

An initial collection attempt lacked Anthropic in the isolated environment. Public
wheels already retained from the earlier CI artifact supplied that dependency; pytest
was linked from the preinstalled local environment. One test's connection mock then
used `uri` as both positional and keyword names; it was corrected before the final
run. Import-order/shebang lint findings were corrected. These preliminary failures
remain in local evidence and are not represented as production incidents.
