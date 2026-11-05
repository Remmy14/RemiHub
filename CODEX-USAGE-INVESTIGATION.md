# Codex usage data — preliminary findings
Verified 2026-11-04.
Preferred source: authenticated Codex app-server account/rateLimits/read with account/rateLimits/updated notifications, documented at https://learn.chatgpt.com/docs/app-server .
Use usedPercent, windowDurationMins and resetsAt; identify five-hour/weekly windows by actual durations (300/10080 minutes), never assume primary/secondary semantics. Support multiple limit IDs, null/unavailable values, account/model bucket identity, fetched-at timestamp and stale data. Remaining percentage is 100-usedPercent when a valid percentage is present.
Run token counts and local session histories are activity evidence, not authoritative remaining subscription quota. Do not scrape private web endpoints or expose authentication files. Do not consume reset credits or send account emails.
Server CLI is 0.162.1. Current online protocol may differ; installed-version protocol/schema support still requires verification. alex development account is not authenticated. No account-limit request or production credential access was made. Production agent quota needs an explicitly designed read-only integration under its existing credential boundary, exporting only sanitized quota snapshots. Never copy its auth to a web backend or development workspace.
Next after review-agent implementation: inspect installed app-server schemas, test with development login, then propose a minimal credential-isolated collector, bounded polling/backoff, admin-only API/cache and UI stale/error states. No service or dashboard changes made.

## Installed protocol investigation, 2026-11-04

Completed after development implementation of independent review. Ran the installed
0.162.1 binary's `--version` and `app-server generate-json-schema --out …` offline,
with an empty temporary Codex home, minimal environment and output under `/tmp`.
No app-server session, model turn, login, account read, rate-limit request, reset
credit redemption or production credential access was performed. The generator
warned that helper aliases cannot be created under `/tmp`; it exited successfully
and produced the schema files. This is schema verification, not authenticated
account validation.

Exact generated files, their SHA-256 manifest and two RPC envelope excerpts are
in [docs/codex-usage-schema/0.162.1](docs/codex-usage-schema/0.162.1/manifest.json).
They came from the installed binary without `--experimental`; the SDK's bundled
schema is not being substituted for the installed CLI schema. The envelope
excerpts retain references into their original bundles; the three complete
individual schema files are self-contained.

Verified installed contracts:

- `account/rateLimits/read` accepts omitted/null params or an object. A background
  collector can send `{"excludeResetCreditDetails": true}`. Do not set
  `supportsLunaReserve`: it advertises automatic fallback support and can record
  experiment exposure after ordinary usage is blocked.
- The full response requires `rateLimits` (the legacy single-bucket object).
  `rateLimitsByLimitId` is an optional nullable map and must retain separate bucket
  identities. Optional nullable `accountId` identifies the account when supplied;
  absent identity must not be guessed from an email or another account's cache.
- A bucket has optional nullable `limitId`, `limitName`, `normalModelSlug`,
  `primary`, `secondary`, `planType`, `credits`, `individualLimit`,
  `spendControlReached`, and `rateLimitReachedType`. A bucket can be empty.
- A present window requires integer `usedPercent`; `windowDurationMins` and
  `resetsAt` can be missing/null. The schema does not constrain percentages to
  0–100. Reject invalid/out-of-range numbers in a future display adapter rather
  than displaying an invented remaining percentage. Recognize 300/10080-minute
  windows by duration; unknown durations remain distinct and labeled unknown.
- `ordinaryUsageAllowed` is optional/nullable. Null means unavailable; never
  infer recovery from percentages or reset times. Spend-control state is also
  independent of quota percentage. `individualLimit` carries string-valued
  `limit`/`used`, integer `remainingPercent` and integer `resetsAt`.
- `account/rateLimits/updated` requires only `rateLimits`, a single sparse bucket.
  It is **not** the full response or bucket map. Preserve account identity and
  other omitted metadata, match only a known bucket identity, and refetch on
  ambiguous identity. `spendControlReached: null` means unavailable, not recovery.
  A full read should replace the cached snapshot; notification merge rules must
  not fabricate a pass/available state from nulls.
- `rateLimitResetCredits` and an untyped `rateLimitUpsell` may appear on a full
  response. They are unnecessary for a basic quota monitor. No reset-credit
  mutation or backend banner interpretation is needed.

The current [official app-server documentation](https://learn.chatgpt.com/docs/app-server)
provides broader protocol context; the captured 0.162.1 output is the versioned
compatibility evidence here. A collector still needs an authenticated development
fixture/probe, bounded polling with backoff, freshness/error semantics, an
account-bound cache, and an administrator-only sanitized API. It must run inside
the existing authentication boundary and export only sanitized data. No collector,
service, account integration or dashboard was installed or changed in this task.
