# Deployment-script portability and verification

Continuation of draft PR #126 from `a7fcf09275d5556fd0db74625f8ac58bce6b4e84`.
This changes the deployment script, its isolated fixtures/tests and portability CI.
No daemon, broker, AI, portfolio or trading-risk implementation is changed.
No real deployment, credential change, broker call or live-money activation occurs.

## Reproduced failure

The original Mac deployment suite returned 13 failures and 6 passes. Bash 3.2
rejected `declare -A` after the stubbed Compose build/start, so container checks
could not complete. GNU-style `sed -i` also did not provide portable repeat stamping.
A further fixture issue was verified directly: `${*##* }` did not select only the
final argument under the Mac system shell. The Docker stub now explicitly walks
its arguments. Existing deployment test functions and assertions are unchanged.

## Repair

Aligned numeric indexed arrays retain each service's container and initial restart
count without associative arrays or service names used as arithmetic subscripts.
Failed service enumeration, duplicate/invalid names, multiple containers, failed or
malformed inspection, invalid restart counters and decreasing counters all refuse.
The original stopped, restarting, restart-loop and unhealthy checks remain enforced.
A bounded integer settle interval is validated before Git/Docker operations.

Revision stamping uses existing Python 3 rather than platform-specific `sed -i`.
It preserves unrelated bytes, normalizes canonical revision lines to one stamp,
and handles CRLF and missing final newlines. A same-directory private temporary
file is flushed before replacement; file mode, owner and group are preserved.
Symlinks, hardlinks, nonregular files and non-0600 environment files refuse.

An interrupted write before replacement leaves the original environment intact.
An ordinary exception removes the temporary file. Abrupt termination may leave a
private temporary file; the dirty-checkout gate then refuses another deployment.
Tests simulate both paths inside disposable repositories, never the user's .env.
No environment value is printed by the stamping code.

## Verification

The focused suite has 51 cases: 19 existing cases plus 32 new regressions.
New CI matrix jobs run those tests on Ubuntu and macOS with /bin/bash selected
explicitly, using fake Docker and throwaway local Git remotes. They do not start
Docker services or require broker credentials. JUnit artifacts retain both results.
The complete Mac baseline, candidate, source hashes, sabotage experiments and
exact-head CI evidence are retained in the PR certification checkpoint.

## Scope and limits

This is not a transaction spanning Git, environment stamping and Docker rollout.
The script does not acquire a cross-process deployment lease or implement rollback.
File replacement is not a guarantee against hardware failure; filesystem-specific
ACL/xattr preservation, recovery and target-host acceptance need separate review.
The stamp operates on canonical physical-line assignments, not a general dotenv
parser. Existing `health=starting` remains accepted as running, not fully ready.
No market calendar, regular-hours override or trading admission rule was changed.

The four institutional risk findings and their failing CI acceptance remain open.
Full daemon integration, migration/recovery, market/settlement coverage, authentic
data and forward performance, and human/target-host acceptance remain separate gates.
