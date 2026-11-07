# Public routes per MEMBERSHIP_FLOW

`MEMBERSHIP_FLOW` is read and validated at backend startup. Values are `legacy` (default),
`v2`, or `off`; restart the backend after changing it. Any other value prevents startup.
`off` means a TO handles membership manually. Modes never fall back silently.

This table is the input for the public nginx allowlist. Status codes assume a valid request,
an active synthetic event in tests, and an existing resource where the route requires one.
The membership mode changes registration; it does not disable check-in, status, or game updates.

| Method | Public path | legacy | v2 | off |
| --- | --- | --- | --- | --- |
| GET | `/` | 200 | 200 | 200 |
| GET | `/kiosk` | 200 | 200 | 200 |
| GET | `/register` | 200, legacy form | 200, mode exposed to template | 200, TO notice replaces form |
| GET | `/status/*` | 200 | 200 | 200 |
| GET | `/static/*` | 200 | 200 | 200 |
| GET | `/health` | 200, `membership_flow=legacy` | 200, `membership_flow=v2` | 200, `membership_flow=off` |
| POST | `/api/checkin/orchestrate` | 200 | 200 | 200 |
| GET | `/api/participant/*/status` | 200 | 200 | 200 |
| PATCH | `/api/player/games` | 200 | 200 | 200 |
| POST | `/api/ebas/register` | 200 success or 202 processing | 410 `flow_v2` | 503 `flow_off` |
| POST | `/api/membership/*` | 404 | 404 until implementation in 1.1 | 404 |
| GET | `/member/confirm/*` | 404 | 404, reserved for 1.1 | 404 |

`POST /api/ebas/register` must remain in the public allowlist for rollback to `legacy`.
In `v2` and `off`, the backend rejects it before reading the request body, touching storage,
or contacting n8n. nginx should forward it so callers receive the documented mode response.
The entire `/api/membership/*` and `/member/confirm/*` families are reserved in the allowlist;
their application handlers do not exist yet. Future v2 handlers must remain unavailable in
`legacy` and `off`. The matrix's strict expected failure records that future v2 work.

Unknown resources can return 404 and invalid payloads can return 400 independently of mode.
The existing `/register.html` backend alias receives the same template mode as `/register`.
The eventual public proxy rejects routes outside this table; internal routes use their own auth.

## Status by id

`POST /api/checkin/orchestrate` additionally returns `status_token` for its `checkin_id`.
The kiosk stores that capability in its session and uses
`GET /api/checkin/{checkin_id}/status?t={status_token}`.
A successful lookup returns only `{found,ready,missing,member,startgg,payment_ok,games,status}`.
The server looks up the exact record ID in the active event and uses the canonical check-in
status computation; stored status text and name/tag fallbacks do not authorize this endpoint.
Email, phone and personal ID fields are never returned.

Tokens contain the check-in ID, an expiry and an HMAC-SHA256 signature, encoded as unpadded
base64url. Their default lifetime is six hours. Missing, invalid, tampered, expired or
different-ID tokens return 403 `{found:false,ready:false}` before storage is queried.
A correctly signed token for an unknown or non-active-event ID returns 200 with the same false flags.
`STATUS_TOKEN_KEY` selects a persistent signing key. If absent, the server derives a distinct
status key from a configured private server secret. If none exists, a random per-process key
is generated with a WARNING that capabilities do not survive restarts.

`/status/{name}` accepts optional `checkin_id` and `t` query parameters and adds template
context `checkin_id`/`status_token` only when the capability verifies for that ID.
The legacy name-based `/api/participant/{name}/status` is deprecated for the kiosk;
its behavior remains available while the pages switch to the capability endpoint.

## Membership v2 endpoints (T-014)

These implemented v2 routes supersede the earlier reserved-route placeholders above.
Every route returns 404 in `legacy` and `off`, before reading the body or contacting storage/providers.
Bodies and responses are JSON. No response includes personal IDs, codes, raw contacts, SPAR names,
provider text, request echoes, ciphertext or code hashes.

- `POST /api/membership/start` accepts `personnummer`, `email`, `phone`, `channel` (`sms|email`),
  `accepted_bylaws`, `accepted_privacy`, `bylaws_version`, `privacy_version`, and `checkin_id`.
  Both consent values must be the JSON boolean `true` (400 `consent_required` otherwise).
  Current versions are `2026-11-06` and `2026-11-06-ebas+app-v1`, defined together in
  `backend/membership_api.py`; stale versions return 409 `stale_texts`.
  Personal IDs require 12 digits and Luhn validation. Email is validated; Swedish national phone
  numbers are normalized to `+46`, and international numbers must use E.164.
  Details are dry-run validated by eBas; only field names leave the backend on 422 `invalid_details`.
  eBas unavailability returns 503 `ebas_unavailable`.
  A code, encrypted attempt context and consent receipt are stored before delivery.
  Delivery failure marks the attempt failed and returns 502 `send_failed`.
  Success is 200 `{ok:true,state:"code_sent",attempt_id,masked_destination,resend_after_s:60}`.
- `POST /api/membership/verify` accepts `{attempt_id,code}`. A wrong code returns 200
  `{ok:false,error_code:"wrong_code",tries_left}`; five wrong codes lock the attempt.
  Expired/locked attempts return the matching stable error code. Atomic `begin_submit` gives
  exactly one caller permission to submit. Concurrent followers return 200
  `{ok:false,state:"submitting"}`; they do not call eBas again.
  Submission uses today's join date. A submitted result finishes the attempt, erases its secrets,
  and sets the associated check-in's member flag through the status-recomputing storage API.
  Rejection becomes failed. In-progress, uncertain or unavailable submissions become outcome_unknown.
  After 20 seconds, an unfinished submission returns 202 `{ok:true,state:"submitting"}`;
  a referenced and shielded background task completes it while the UI polls status.
- `POST /api/membership/resend` accepts `{attempt_id}`. A successful atomic resend rotates the code
  without resetting the five-strike counter, and returns the same shape as start.
  The store enforces the 60-second cooldown and contact send quota. 429 `rate_limited` includes
  numeric `retry_after_s` and a matching `Retry-After` header.
- `GET /api/membership/{attempt_id}/status` returns the current state and a stable error code
  when needed. Unknown or malformed attempt IDs return 404.
  For outcome_unknown, a durable store claim permits at most one membership confirmation per
  attempt every 30 seconds. Found membership becomes submitted and updates the check-in.
  A negative or unavailable confirmation remains outcome_unknown; the client never resubmits it.
  At the 15-minute PNR deadline the store transitions it to manual_review, erases secrets,
  and the API returns `state:"manual_review",user_message:"Ask a TO to check your membership"`.
  Neither a negative lookup nor an uncertain outcome sets the check-in's member flag to false.

Response keys are restricted to `ok`, `state`, `error_code`, `user_message`, `tries_left`,
`attempt_id`, `masked_destination`, `resend_after_s`, `retry_after_s`, and `field_errors`.
`field_errors` contains only whitelisted field-name strings; it never contains provider messages.
All user messages are English. The random attempt ID is the capability for status/verify/resend;
there is no cross-attempt lookup by name, contact or personal ID.
A lightweight router lifespan task calls `cleanup_expired()` every 60 seconds in every mode.
An independent background loop reconciles pending attempts with the store's 30-second claim gate;
switching the public membership mode never drops in-flight submissions or reconciliation.
Consent receipts survive attempt-secret cleanup. Application diagnostics contain only attempt ID,
state and stable error code; no submitted data or provider exception text is logged by the router.

The swappable `shared/verification_service.py` boundary is app-owned-code issuance today:
`start_verification(attempt_id, channel, destination, *, code=None)` returns internal
`{challenge_ref,expires_at,resend_policy}`. The endpoint first generates/stores an initial code,
then passes it as the optional keyword; a bare three-argument call is a durable resend.
`check_verification(attempt_id,challenge_ref,code)` returns a `VerificationCheck` with status
`verified|invalid|expired|locked|unavailable` and a private, non-represented `submission_claim`.
It performs exactly one atomic `begin_submit`; the winning claim is passed directly to the
submit task, with no plaintext cache or second claim. Provider-owned eBas codes will need their
own issuance path; that contract remains open until the meeting with Mattis.

Both `/register` and `/register.html` receive `membership_year` equal to the current renewed year.
An optional query-string `checkin_id` is passed to the template only after UUID validation;
absent or invalid values are omitted. Start validates that a supplied check-in belongs to the active event.

Membership attempts expose `membership_year` from the immutable consent receipt recorded at start.
Verify and the worker handoff both require the server's current year to match that consented year;
a mismatch returns 409 `year_changed` with `user_message:"Please start again"`, erases an unsubmitted
challenge's secrets and requires fresh consent. A worker captures one renewed date and uses it only
when its year matches; reconciliation always uses the stored membership year across calendar changes.

The background reconciler first atomically recovers abandoned `submitting` claims older than
`EBAS_SUBMIT_TIMEOUT_SECONDS + 60` seconds to `outcome_unknown`, then uses confirmation only.
It never retries submission. Failed worker creation marks the claim uncertain immediately.
At the fixed fifteen-minute deadline, both submitting and outcome_unknown become manual_review
and their secrets are erased; cleanup uses database time. A retained worker claim cannot submit
after the store's hard deadline. Started worker/maintenance tasks are drained on graceful shutdown.

## Orchestrate response: integrations/degraded

`POST /api/checkin/orchestrate` additionally returns:

```json
{"integrations":{"ebas":"ok","startgg":"ok"},"degraded":false}
```

Each integration is `ok` (a provider result is available, including a verified negative),
`unavailable` (the call failed or its result is ambiguous), or `skipped` (it did not run).
`degraded` is true when any source is unavailable. With `INTEGRATION_ENGINE=none`, both
sources are `skipped` and `degraded` is explicitly true because integrations are disabled.
Timeouts, HTTP failures and malformed summaries report both sources unavailable.
Availability reporting performs no additional provider calls or writes to provider flags.

The T-028 n8n summary's `results.ebas` and `results.startgg` states take precedence over
other explicit per-source `integrations` values and transport-based legacy inference.
`results.attempt_id` is correlation metadata and is not exposed in the availability response.
Source reports under `ebas`/`startgg` (or under `results`/`integrations`) may contain a `state` field:
`verified_true` and `verified_false` both map to `ok`; `unavailable` and `skipped` retain
their meanings. Unknown or malformed explicit states are unavailable. A source skipped because
it is not required does not itself make the response degraded. No database state shape is required.
For legacy summaries, `integration_complete:true` with a positive `member`/`startgg` result
establishes availability; a negative is ambiguous because the old workflow also emits false
on errors. Without a personal ID, a non-explicit eBas result is skipped.

The check-in UI derives the membership signup hint from `member === false` or `"Membership"`
in `missing`. When eBas is unavailable or skipped, the response uses `member:null` for an
unverified stored membership and omits `"Membership"` from the response's `missing` array.
A previously verified `member:true` remains true. Clients must treat null as unknown and
use `degraded`/`integrations` to offer a retry or TO assistance rather than membership signup.
These response hints do not change canonical readiness, stored status, or stored flags;
`ready:false` may therefore accompany an empty response `missing` array during an outage.

## Membership start: check-in ownership

`POST /api/membership/start` additionally requires `checkin_id` and `status_token` in the
JSON body. The token is the T-024 status capability issued for that exact check-in ID;
the UI carries it as `t` between pages and sends it as `status_token` on start.
Missing, invalid, expired, tampered or different-ID capabilities return
403 `{ok:false,error_code:"not_owner",user_message:...}` before attempt creation, provider
validation, rate-limit lookup or code delivery. A valid capability still requires the
check-in to exist in the active event; otherwise start returns 404 `not_found`.
The token is checked at start and is not stored in membership attempts or returned in
membership responses. Existing consent, validation and membership-mode checks still apply.

## Active-event write serialization

Active check-in writes lock `hashtext('fgt-event:' || event_slug)` before row locks.
Single-row updates/deletes and check-in creation use shared event locks; archive, reopen,
status recomputation and player merge/undo use exclusive event locks. A row update that
moves events locks both source and destination in sorted order. ID-only writers discover
the slug without row locks, then lock the event and recheck the original row before writing.
Player merge/undo acquire affected event slugs in sorted order before the global player-merge
lock. Check-in creation, updates and restore share that global lock after their event locks.
The merge lock phase rechecks its event coverage and retries its lock-only savepoint if a
new affected event appeared while waiting, up to three attempts before a visible error.
Merge/undo use a 10-second transaction lock timeout; ordinary writers share the global lock.
No active row is written under an uncovered event. See `docs/locking.md` for the lock table.

## Public capability issuance and legacy registration ownership

`POST /api/checkin/orchestrate` issues `status_token` for a newly created row. For a repeat
check-in, it requires either a non-empty matching telephone (digits normalized to Swedish
`46` form, accepting `0`/`+46`/`0046`) or a non-empty matching email (trimmed, lower-case).
Both comparisons use constant-time byte comparison against the locked pre-update row.
The database returns the ownership verdict atomically with begin/upsert; no stored contacts
are included in the response. A concurrent insert conflict receives the same existing-row
proof check and cannot claim to be a new creation.

An unverified repeat still returns its check-in result, with `ownership:"unverified"` and
no `status_token`. The UI must show "Ask a TO to help you continue". Its stored name, email,
and telephone remain unchanged, preventing a failed request from establishing proof for a
second request. Acquisition source may fill an empty value. Verified repeats retain the
existing safe-field update rules; membership, registration and payment flags remain protected.

In legacy mode with integrations enabled, `POST /api/ebas/register` additionally requires
`checkin_id` and its `status_token`. Invalid/missing/expired/foreign capabilities return 403
before any n8n call, including the already-member shortcut. The backend acts only on the
signed row in the active event (404 if absent/inactive); optional tag/slug fields must match
that row or return 403. Tag/slug never select a different target. The capability is not
forwarded to n8n. Existing membership-mode and disabled-engine gates retain their precedence.

An unverified repeat also returns `reason:"ownership_unverified"` and
`integrations.ebas:"skipped"`. Its n8n payload omits `personnummer` entirely and records
callback authority for Start.gg only. An eBas callback using that run ID is rejected as
stale; any previously authorized eBas run remains unchanged. Start.gg continues normally.

`POST /api/checkin/begin` uses the same public ownership rules unless `X-Internal-Token`
matches the configured, non-empty `INTERNAL_API_TOKEN` using constant-time comparison.
An unset/empty server token never grants trusted behavior. Missing or wrong headers leave
the route in public mode even though its soft-auth dependency permits the request.
Neither flat nor nested payload flags can claim trusted ownership. A valid internal token
permits the existing trusted safe-field updates; begin itself does not issue a capability.

## Check-in eBas confirmation runs in Python

Check-in orchestration never includes `personnummer` in the n8n v5 payload, for either
verified or unverified ownership. n8n receives only a Start.gg-scoped attempt ID.
The v5 eBas branch remains in the exported workflow but has no PNR/work to perform;
its eBas summaries are ignored, and its Start.gg attempt cannot authorize eBas writes.

For a verified owner with a personal ID and enabled integrations, Python confirms the
current year's membership through `create_ebas_client`, off the event loop. Its distinct
eBas-scoped attempt is applied through `apply_integration_result`, preserving stale-run
rejection, membership provenance and the event/global/row lock order. Found/not-found
map to verified true/false; provider or transport errors map to unavailable, never false.
The response keeps the existing `integrations`/`degraded` shape; native eBas availability
replaces n8n's eBas telemetry. Missing PNR, unverified ownership, or disabled integrations
skip the check. The disabled engine makes no eBas or n8n calls.

The private check overlaps the n8n wait within the existing orchestration timeout budget.
If its result arrives after that deadline, the response reports unavailable; any late
write still uses the same stale/provenance guards. PNR references are discarded after
confirmation and are never passed to the writer, logs, error messages, or response.

The legacy `/api/ebas/register` path still sends PNR to n8n as the user's explicitly
accepted temporary exception (2026-11-07 decision). It is outside this check-path change.

Confirm and validate accept only successful HTTP status codes. Confirm also rejects any
`error`/`errors` field or non-success `request_result`; an error envelope containing
`member_found: false` is unavailable and cannot change stored membership.
The default stub confirmation returns `stub_transport`/unavailable. Synthetic tests may
explicitly choose `EBAS_STUB_CONFIRM=found|not_found` in DEV. Production ignores that opt-in,
keeps checks unavailable, and logs one startup ERROR when the stub is selected; a real
backend check requires the deliberately configured live transport.

Before the client call, 10-digit PNRs use the workflow's fixed century cutoff: YY > 30
means 19YY, otherwise 20YY. A `-` preserves that rule; `+` selects a century at least
100 calendar years old. Explicit 12-digit centuries are preserved. PNR is never logged.

## Membership lookup state records the last accepted eBas outcome

`member_lookup_state` is read-only lookup metadata, separate from authoritative `member`:
`confirmed_member` means a successful explicit true result; `confirmed_nonmember` means
a successful explicit false result; `unknown` means a technical error or unusable result;
`skipped` means an explicit skipped result accepted by the canonical integration writer.
NULL on older/unrecorded rows reads as `unknown`, without a backfill. Status exposes only
this enum string, never a provider reason or response body.

Only a current accepted eBas result changes this metadata. Orchestration paths that skip
the writer (missing PNR, unverified ownership, disabled integrations) keep the prior value,
as do response-only deadline expiries until the native worker finishes. Same-event
re-check-ins also keep `member`. Errors/skipped results never change membership, confirmed
negatives preserve protected TO/v2 truth, and lookup results never close in-flight v2 attempts.

## eBas check webhook used by member-card (T-048)

`POST /webhook/ebas/check` retains its existing webhook path, method and authentication.
The existing 10/12-digit personal-ID normalization and execution-payload privacy settings
are unchanged. The provider call has an explicit 8-second timeout, below member-card's
15-second HTTP timeout; provider transport/HTTP/JSON errors use a separate error output.

Only a successful provider response containing an explicit boolean `response.member_found`
can return HTTP 200 JSON. Both positive and negative results keep the existing payload:
`{ok:true,state:"verified_true"|"verified_false",isMember:true|false,attempt_id:...,message:...}`.
The messages remain `Medlem i Sverok` and `Ej medlem i Sverok`, respectively.
Any `error` or `errors` field (including nested or empty-valued fields), a present
`request_result` other than `"success"`, non-2xx status, timeout or missing/malformed
membership result instead returns HTTP **503**, header
`Content-Type: text/plain`, with the literal **non-JSON** body `ebas_unavailable`.
Missing/invalid input and processing-node errors also use this fixed response. No personal
ID, provider payload or membership boolean is included in it.

The non-JSON error body is deliberate compatibility behavior: member-card's
`backend/main.py:78-97` posts with a 15-second timeout and calls `resp.json()` inside its
`try`, without checking HTTP status. A JSON error object would fall through to
`result.get("isMember", False)` and incorrectly produce its not-member 404. Plain text
makes JSON decoding fail, reaching its existing 502 retry message
`Kunde inte verifiera medlemskap just nu. Försök igen.`. Verified false still produces
`Inget aktivt medlemskap hittades. Kontakta styrelsen.`; verified true continues to the
unchanged player/card processing. Member-card and meetup code/data are not modified.
Callers may additionally use `raise_for_status()` before JSON decoding as hardening;
this is compatible with the same 503 contract and is not required for this change.
