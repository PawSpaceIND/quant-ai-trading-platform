# Zerodha renewal: keep dotenv field identity unambiguous

## Scope

Independent follow-up on draft PR #129, based on
`d0dc5d203b3946eb8294157716bff10056080fbf`. This change repairs the owner
renewal parser. It does not resolve the previously reported Gitleaks finding.
It does not merge, deploy, refresh services, perform a real provider request,
read production credentials, change risk gates or add live-money authority.

## Reproduced defects

The old publisher treated Docker's environment file as isolated shell lines.
It recognized only `=` and parsed managed values using `shlex`. Docker documents
`:` as another delimiter, a different unquoted-comment rule, and quoted multiline
values. These differences were observable with synthetic data:

- A colon-delimited account or mode field was ignored by the continuity or
  paper-only checks. Mixed-delimiter duplicates were not detected.
- An assignment-shaped line inside an unrelated quoted value could be mistaken
  for a top-level managed field and replaced within that unrelated value.
- Shell backslash removal or adjacent-quote concatenation changed the scalar
  checked by the helper, while an unspaced `#` was wrongly treated as a comment.

Authority checked against Docker's environment-file syntax documentation:
https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax

## Repair and deliberate refusals

One shared line validator now governs both field extraction and publication.
Both documented delimiters feed the same duplicate/account/mode checks. Managed
values use a small literal-scalar parser instead of shell interpretation.
An unquoted `#` is a comment only after whitespace. An unrelated one-line JSON
value, escaped quoted value, comment or ordinary line is retained byte-for-byte.
The atomic same-directory replacement and private-file checks are unchanged.

This is deliberately not a complete Docker dotenv interpreter. Multiline values
are valid Docker syntax but unsupported by this updater: the whole file refuses
before interactive login or publication. Unsupported assignments, control/Unicode
line separators, trailing quoted data and managed values needing interpolation,
escape decoding, embedded quotes or whitespace likewise refuse. Move unsupported
content out of the renewal input or represent it as an unambiguous single-line
value; the helper never silently drops it or edits within it. Existing real host
file compatibility has not been observed, because no production file was read.

## Executed evidence

- Fresh complete base: **1596 passed, 13 failed**, 2 warnings, 14 subtests passed.
- Complete candidate: **1640 passed, the identical 13 failures**, 2 warnings,
  14 subtests passed. No skips. All 13 are existing Mac deployment-portability
  cases; both JUnit failure-identity sets are retained and compared exactly.
- **44 new synthetic cases**: final regression set against the old source gives
  **35 failed / 9 passed**; repaired source gives **44 passed**.
- Combined renewal/session/login/daemon/container-health coverage: **163 passed**.
- Ruff `src tests scripts/renew_pilot_token.py` and whitespace checks pass.
  `/usr/local/bin/ruff` is absent on this Mac; the absolute existing project
  virtual-environment interpreter is used, not an implicitly selected binary.
- All **421 source/test/runtime files** remain hash-identical across the full run.
- **8/8 mutation checks caught** with assertion failures and zero collection
  errors. Every mutant ran in a disposable source copy with isolated bytecode
  caching; the working production source was never mutated.

The exact results are in `docs/evidence/zerodha-env-syntax-suite.json` and
`docs/evidence/zerodha-env-syntax-sabotage.json`. JUnit counts include 14 subtests,
so they must not be presented as the ordinary pytest case count.

| Deliberate mutation | Regression that turned red |
| --- | --- |
| Stop recognizing colon assignments | `test_documented_delimiters_have_identical_field_authority` |
| Accept unterminated/multiline quotes | `test_unterminated_foreign_value_refuses_before_any_provider_call` |
| Ignore unsupported assignments | `test_unsupported_assignment_syntax_is_not_silently_ignored` |
| Remove control-character refusal | `test_non_line_ending_controls_cannot_create_assignments` |
| Misread escaped closing quotes | `test_unrelated_escaped_quotes_remain_byte_identical` |
| Accept data after a quoted value | `test_quoted_scalar_cannot_hide_trailing_value` |
| Interpret managed scalar escapes/interpolation | `test_shell_only_or_interpolated_credentials_are_not_reinterpreted` |
| Treat every hash as a comment | `test_unquoted_hash_without_space_is_literal_not_a_shell_comment` |

## Release status remains separate

This evidence is synthetic local engineering verification, not exact-head Linux
CI or target-host acceptance. The earlier security-scan finding remains unresolved;
no scanner exception, scan modification or finding classification was introduced.
A later CI/ref metadata refresh was tool-blocked; no alternate inspection route
was used to evade that block. Require the actual published-head CI result before
review completion. Keep the PR draft and unmerged. Real daily login/2FA, target-host
publication and service recreation, alert delivery, and items 2-4 remain open.
