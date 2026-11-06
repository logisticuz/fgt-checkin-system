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
