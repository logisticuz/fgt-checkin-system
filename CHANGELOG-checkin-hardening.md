# Changelog: feat/checkin-hardening

This document summarizes all changes made in the `feat/checkin-hardening` branch since the last release.

---

## Overview

This release focuses on **security hardening**, **input validation**, **language consistency** (Swedish -> English), and **architectural improvements** to the check-in polling system.

---

## 1. Security Hardening

### 1.1 Webhook Token Authentication
- **File:** `backend/main.py`
- **Change:** Added `N8N_WEBHOOK_TOKEN` environment variable
- **Why:** Protects webhook endpoints from unauthorized external calls
- **How:** Token is validated on all `/n8n/webhook/*` requests via query param or header

### 1.2 Input Validation & Sanitization
- **New files:**
  - `backend/validation.py` (server-side)
  - `backend/static/js/validation.js` (client-side)
- **Features:**
  - Max length enforcement on all fields (prevents DB overflow attacks)
  - Phone number normalization: "070-123 45 67" -> "0701234567"
  - Personal ID (personnummer) normalization: "19900101-1234" -> "199001011234"
  - Whitespace trimming (prevents "Viktor " vs "Viktor" matching issues)
  - Format validation (personnummer must be 10 or 12 digits, phone min 7 digits)
- **Why:** Defense in depth - validate both client-side (UX) and server-side (security)

### 1.3 Sensitive Data Cleanup
- **File:** `backend/templates/status_pending.html`
- **Change:** `localStorage.removeItem()` for personnummer and phone on successful check-in
- **Why:** Do not leave sensitive data in browser storage after it is no longer needed

---

## 2. Architecture Changes

### 2.1 New Status Polling API
- **File:** `backend/main.py`
- **New endpoint:** `GET /api/participant/{name}/status`
- **Why:** Previous architecture polled n8n webhooks, but n8n does not know about payment status (stored in Airtable). This caused users to get stuck on the "Fetching status..." page.
- **How:** Backend now reads directly from Airtable and returns unified status JSON
- **Response format:**
  ```json
  {
    "ready": false,
    "status": "Pending",
    "missing": ["Payment", "Membership"],
    "member": false,
    "payment": false,
    "startgg": true,
    "name": "Viktor",
    "tag": "VikThor",
    "startgg_events": ["SF6", "T8"]
  }
  ```

### 2.2 Updated Polling Client
- **File:** `backend/templates/status_pending.html`
- **Change:** Now polls `/api/participant/{name}/status` instead of n8n webhook
- **Added:** Polling timeout (max 30 polls = 5 minutes), duplicate request guard (`inFlight` flag)

### 2.3 Dynamic Settings from Airtable
- **File:** `backend/main.py`
- **New function:** `get_active_settings()`
- **Why:** Swish number and price per game should be configurable without code changes
- **Reads:** `swish_number`, `swish_expected_per_game` from Airtable `settings` table

### 2.4 Payment Status API
- **File:** `backend/main.py`
- **New endpoint:** `PATCH /players/{record_id}/payment`
- **Why:** Allow TO dashboard to mark Swish payments as verified
- **Body:** `{ "payment_valid": true }`

---

## 3. Language Consistency (Swedish -> English)

### 3.1 Backend Code
- **Files:** `backend/main.py`, `backend/validation.py`
- **Changes:**
  - `namn` -> `name`
  - `medlem` -> `member`
  - `swish` -> `payment`
  - `saknas` -> `missing`
  - `klar` -> `ready`
- **Backward compat:** Swedish input keys still accepted, converted to English internally

### 3.2 Frontend Templates
- **Files:** All templates in `backend/templates/`
- **Changes:**
  - All UI text now in English
  - Element IDs: `id="namn"` -> `id="playerName"`, `id="match-medlem"` -> `id="match-member"`
  - Error messages in English

### 3.3 JavaScript Validation
- **File:** `backend/static/js/validation.js`
- **Changes:** All error messages now English
  - Swedish "Namn kravs" -> "Name is required"
  - Swedish "Telefonnummer for kort" -> "Phone number too short"

### 3.4 Shared API
- **File:** `shared/airtable_api.py`
- **Change:** `gametag` -> `tag` (standardized field name)

---

## 4. Field Naming Standardization

### 4.1 `tag` is the Standard
- **Decision:** Use `tag` everywhere (not `gametag`, `gamerTag`, or `nick`)
- **Reasoning:**
  - Shorter and clearer
  - Avoids confusion with Start.gg's "gamerTag" (which is their specific field)
  - Our `tag` is the player's preferred display name at our events
- **Files changed:** `shared/airtable_api.py`, `backend/main.py`, n8n flows
- **Manual action required:** Rename `gametag` column to `tag` in Airtable UI

---

## 5. Documentation

### 5.1 New Documentation
- **Locations:** `docs/` (Swedish) and `docs-en/` (English)
- **Files:**
  - `0_Problem_och_Losning.md` / `0_Problem_and_Solution.md`
  - `1_Arkitektur.md` / `1_Architecture.md`
  - `2_Datafloden.md` / `2_Data_Flow.md`
  - `3_Installation_och_Setup.md` / `3_Installation_and_Setup.md`
  - `4_Externa_Beroenden.md` / `4_External_Dependencies.md`
  - `5_Forbattringsforslag.md` / `5_Improvement_Suggestions.md`
  - `6_API_Referens.md` / `6_API_Reference.md`
  - `7_Datamodell_Airtable.md` / `7_Data_Model_Airtable.md`
  - `README.md` - Documentation index for both sets

---

## 6. Infrastructure

### 6.1 New Files
- `nginx/` - Nginx configuration for production
- `airtable_templates/` - Airtable base templates/schemas
- `backend/static/js/` - Client-side JavaScript (validation)
- `docs-en/` - English documentation set
- `CHANGELOG-checkin-hardening.md` - This changelog

### 6.2 Updated Files
- `docker-compose.prod.yml` - Updated service configuration
- `.env.example` - Added `N8N_WEBHOOK_TOKEN`

### 6.3 Removed Files
- `backend/data/*.csv` - Old CSV data files (now using Airtable)
- `dashboard/api.py` - Moved to shared module
- `docker-compose-override.yml` - No longer needed

---

## 7. Files to Commit

### Modified (tracked)
- [x] `.env.example`
- [x] `backend/main.py`
- [x] `backend/templates/checkin.html`
- [x] `backend/templates/register.html`
- [x] `backend/templates/status_pending.html`
- [x] `backend/templates/status_ready.html`
- [x] `docker-compose.prod.yml`
- [x] `fgt_dashboard/callbacks.py`
- [x] `fgt_dashboard/layout.py`
- [x] `shared/airtable_api.py`

### Deleted
- [x] `backend/data/ebas_medlemmar.csv`
- [x] `backend/data/registreringar.csv`
- [x] `backend/data/startgg_reg.csv`
- [x] `backend/data/swish_logs.csv`
- [x] `dashboard/api.py`
- [x] `docker-compose-override.yml`

### New (to add)
- [x] `backend/validation.py`
- [x] `backend/static/js/validation.js`
- [x] `docs/` (Swedish docs)
- [x] `docs-en/` (English docs)
- [x] `nginx/`
- [x] `airtable_templates/`
- [x] `CHANGELOG-checkin-hardening.md` (this file)

### Exclude from commit
- [ ] `n8n-backup-20250829-1220/` - Backup files, not needed in repo
- [ ] `context/` - Local context files
- [ ] `TASKS.md`, `TODOLIST.md` - Local task tracking

---

## 8. Suggested Commit Message

```
feat(checkin): hardening - status API, validation, English UI

Security:
- Add webhook token authentication (N8N_WEBHOOK_TOKEN)
- Add input validation and sanitization (Python + JS)
- Clear sensitive localStorage data on successful check-in

Architecture:
- Add /api/participant/{name}/status endpoint for reliable polling
- Add PATCH /players/{id}/payment for TO dashboard
- Add dynamic settings from Airtable (Swish config)

Consistency:
- Convert all UI and code to English (keep Swedish backward compat)
- Standardize on 'tag' field naming (not 'gametag')

Docs:
- Add comprehensive system documentation in Swedish and English
- Add Nginx config and Airtable templates

Infrastructure:
- Remove legacy CSV files (now using Airtable)
```

---

## 9. Post-Merge Actions

1. Airtable: Rename `gametag` column to `tag` in both `active_event_data` and `players` tables
2. Environment: Add `N8N_WEBHOOK_TOKEN` to production `.env`
3. n8n: Update flows to use `tag` field (if not already done)

---

## 10. Testing Checklist

- [ ] Check-in flow works end-to-end
- [ ] Status polling shows correct status
- [ ] Validation errors display correctly (client-side)
- [ ] Invalid input rejected (server-side)
- [ ] Webhook token blocks unauthorized requests
- [ ] Dashboard can mark payments
- [ ] Swedish input still works (backward compat)
