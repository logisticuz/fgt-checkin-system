# n8n execution-data hygiene

The four live exports are Checkin Orchestrator v5 (PG), eBas Membership Check,
eBas Register v2 (PG), and Start.gg Check in `n8n/flows/`.
Their workflow settings disable successful/failed payload saving, manual saving, and progress saving.
Backend HTTP header `X-Internal-Token` uses the n8n expression `={{ $env.INTERNAL_API_TOKEN }}`;
draft/published copies are covered. Other executable parameters and connections are unchanged.
The Normalize sticky note uses format placeholders rather than numeric PNR examples.
Set matching private `INTERNAL_API_TOKEN` values for backend and n8n; examples stay empty.
Make that variable available under the instance's existing `$env`/allowlist policy.

DEV import: conductor holds `dev-n8n`, verifies each target workflow ID, and imports one JSON
using the instance's Server CLI: `n8n import:workflow --input="<path-to-one-json>"`.
Matching IDs update existing workflows; check credentials/webhook IDs, then publish the updated version.
Verify the four save settings and test callbacks with synthetic/stubbed data, without logging the token.
PROD import is a PROD-team action after DEV validation, using the same ID/settings checks.

These settings do not remove saved executions from before the import.
The responsible operator opens Overview → Executions → Filters and selects one named workflow.
Select its finished executions, including all filtered pages, then Delete executions/Delete and confirm.
Refresh and repeat until no old finished executions remain; repeat for the other workflows.
Delete execution records, preserving workflows and their webhook IDs.
PROD execution deletion is performed only by the PROD team; DEV agents do not run it.

References: [save settings](https://docs.n8n.io/workflows/settings/),
[Server CLI import](https://docs.n8n.io/hosting/cli-commands/#import-workflows-and-credentials),
[execution filters](https://docs.n8n.io/workflows/executions/all-executions/),
[execution-delete UI](https://github.com/n8n-io/n8n/issues/11115).
