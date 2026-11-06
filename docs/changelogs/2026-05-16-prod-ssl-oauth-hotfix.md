# 2026-05-16 Prod SSL and Start.gg OAuth Hotfix

## Context
- Production runs on the Raspberry Pi.
- Normal workflow is dev computer -> Git -> production pull.
- This session required direct production hotfixes, so code/config changes need to be brought back through Git for the dev computer.

## Runtime Fixes Applied On Production
- Renewed the expired Let's Encrypt certificate for `checkin.fgctrollhattan.se`.
- Reloaded nginx after renewal.
- Verified HTTPS for:
  - `checkin.fgctrollhattan.se`
  - `www.checkin.fgctrollhattan.se`
  - `admin.fgctrollhattan.se`
  - `meetup.fgctrollhattan.se`
  - `membercard.fgctrollhattan.se`
- Fixed the server crontab renewal command to use the active Compose project and non-interactive `exec -T`.
- Verified certbot renewal dry-run.

## Code Fixes
- Updated `shared/auth.py` Start.gg OAuth helper:
  - Use `https://www.start.gg/oauth/authorize`.
  - Encode OAuth scopes with `%20`.
  - Send token exchange as JSON.
  - Include `scope` in the token exchange payload.

## Repo Hygiene
- Documented the dev/prod/hotfix workflow in `AGENTS.md`.
- Added `.gitignore` entries for local production backups and duplicate Airtable CSV exports.

## Verification
- `python -m py_compile shared/auth.py`
- `docker compose -f docker-compose.prod.yml restart fgt_dashboard`
- `docker compose -f docker-compose.prod.yml ps fgt_dashboard` showed healthy.
- `GET https://admin.fgctrollhattan.se/auth/login` redirects to Start.gg OAuth.
