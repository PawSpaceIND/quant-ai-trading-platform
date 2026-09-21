# Atlas testing API budget

The pilot deployment defaults to one combined **$2.50 per UTC day** allowance for
paid Atlas text inference. UTC midnight is 05:30 IST. Set
`PRAMANA_AI_DAILY_USD_LIMIT=2.50` in the private host `.env`; Compose forwards the
same setting and `/data/ai-spend.sqlite` to services sharing the existing data volume.
This requires the new code and container recreation, not just editing `.env` or restarting.
Existing call/token limits still apply independently. Zero or malformed dollar limits
refuse paid calls; zero does not disable this cap.

Covered configured application paths: Anthropic consensus and headlines, OpenAI Astra
comparison, dashboard chat and each of its recovery attempts, research provider calls,
and probes that invoke the production adapters. The Anthropic SDK no longer performs
hidden retries. Requests select standard service tiers. Arbitrary standalone HTTP
scripts, other applications sharing the keys, and instances using another budget
volume are outside this application ledger. Do not run the old standalone AWS chat
probe after enabling this cap; it does not use the shared dollar ledger.

## Admission and accounting

Each request obtains an atomic SQLite reservation before provider I/O. Its allowance
uses a conservative text-input bound, the maximum output including reasoning, and
the reviewed model rates. Python and Node use the same ledger and integer microdollars.
Concurrent processes cannot independently consume the last allowance. Successful
reported usage settles only that request's reservation, exactly once, on its original
UTC day. Unknown completion, missing usage and failed recording keep money reserved.
Reservations survive restarts. Lowering the cap never clears usage; another process
cannot raise that day's stored ceiling.

The initial activation day is closed to new paid inference because earlier requests
did not have complete dollar accounting. This is retained as `carried_micro`, separate
from estimated new spending. Startup initializes the ledger even when markets are
closed. A fresh allowance starts the following UTC day. Never delete the ledger or
change its path to reset spending. Preserve it in the existing volume. The scheduled ledger backup does not automatically
include this separate database: back it up with SQLite's online backup API before
replacing the volume. A restore of an older copy must not be used to reopen today's
allowance; retain the current ledger or keep paid calls disabled for the restore day.

The copilot shows the shared limit, estimated used amount, reservations, remaining
allowance and activation hold. These are conservative API estimates, **not invoices**.
AWS hosting, broker/data subscriptions, taxes, foreign exchange and unrelated account
usage are not included. The provider's billing page remains authoritative for charges.

At the ceiling, paid model requests are refused. The guard does not halt market-data
collection, deterministic research or independent position protection. It does not
change confidence floors, authorize trades or promote Astra. Existing logic still
decides whether deterministic evidence can qualify a paper action.

## Reviewed pricing and supported requests

Checked 21 September 2026:

- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing):
  Sonnet 5 standard input $2/MTok, output $10/MTok; cache writes up to $4/MTok,
  cache reads $0.20/MTok. Reservations use the highest input rate; settlement uses
  recorded input/output/cache categories and the higher write rate for all writes.
- [OpenAI Astra pricing](https://developers.openai.com/api/docs/models/gpt-6-astra):
  standard input $10/MTok, cache write $12.50/MTok, output $50/MTok. The ledger
  conservatively charges all reported Astra input at $12.50/MTok; input already
  includes cached tokens, and reasoning is included in output.

Only these exact model IDs and bounded text requests are priced. Unknown models,
hosted tools, images, unpriced tiers, and long-context-sized requests are refused.
Pricing review expires after 21 October 2026; paid requests then stop until a reviewed
policy update. Future price changes still require review; this software is not a
provider-side invoice guarantee. Standard-tier selection avoids project default
Fast-mode pricing. Keep provider/project spend controls as an independent backstop.

## Validation and rollout

Tests must cover first-day carryover, next-day reset, concurrent Python reservations,
Python/Node shared spending, restart persistence, idempotent settlement, midnight
completion, cached/reasoning tokens, unknown costs, corrupted storage, unpriced
requests, expired pricing, cap changes, and zero provider I/O when held. Retries
must reserve again. Existing trading/protection and strategy-manifest checks remain
required. Mocked calls do not spend API credit.

After merge, deploy outside the configured entry session through the existing host
procedure. Verify both running containers have the same cap and budget volume,
inspect the initial hold and tomorrow's remaining allowance, then make at most one
qualified test through a covered adapter. Do not reset the cap to obtain a passing
probe. Model generation, full dashboard answers and a real-feed paper session still
need their own operational verification.
