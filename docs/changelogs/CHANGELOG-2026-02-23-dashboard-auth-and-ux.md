# Changelog - 2026-02-23 (Dashboard auth + UX polish)

## Scope

Finalized DEV auth flow, TO authorization checks, archive/delete ergonomics,
and Ready-page UX after the Postgres + n8n migration.

## Implemented

- Dashboard OAuth hardening:
  - Require login before dashboard access.
  - Validate TO access against Start.gg during callback.
  - Added fallback flow when no active slug exists (event selection / access denial message).
  - Fixed session cookie path behavior for Dash internal routes.
  - Added short transition screen after Start.gg approve.
- Dashboard landing page UX:
  - New pre-login landing at `/admin/` with branding and clear login CTA.
  - Added subtle motion and visual hierarchy improvements.
- Audit logging improvements:
  - Manual TO actions now log actor identity for delete/toggle actions.
- Archive operations UX:
  - Added quick `Archive Event` action next to Refresh in Live Check-ins.
  - Added permanent "Delete archived event" flow with:
    - required reason,
    - confirmation prompt,
    - audit reason/details,
    - delete from `event_archive` + `event_stats` only.
  - Moved destructive delete flow into Settings -> `Advanced` (collapsible).
- Ready-page polish (`status_ready.html`):
  - Better hierarchy for TO-at-distance visibility and player next-step actions.
  - Kept strong green ready signal.
  - Elevated bracket CTA and simplified duplicate status indicators.
  - Added subtle, non-intrusive button glow animations.
- Payment requirement UX:
  - Hide `No Payment` stats card/filter when payment requirement is disabled.

## DEV verification performed

- OAuth login via `/admin/auth/login` and callback works.
- Dashboard session persists and displays logged-in user.
- Audit entries include manual admin actions under the logged-in user.
- Archive delete endpoint validates missing reason and succeeds with reason.
- `No Payment` card hides when `require_payment` is off.
- Ready page renders updated layout and interactions.

## Notes

- Existing unrelated local changes (legacy docs/backup CSV churn) were intentionally left untouched.
