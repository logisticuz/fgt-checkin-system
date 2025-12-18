# Changelog: 2025-12-18

Session changes for the `feat/checkin-hardening` branch.

---

## Dashboard Redesign

### Esports Theme (`fgt_dashboard/layout.py`)
- Complete visual overhaul with esports color palette:
  - Neon blue: `#58aaff`
  - Purple: `#a855f7`
  - Green: `#22c55e`
- Gradient header with centered logo
- "LIVE" indicator with pulse animation (top right corner)
- Stats cards showing:
  - Total players
  - Ready count
  - Needs attention count
- Modern table styling with alternating row colors
- "Needs Attention" section highlighting players missing requirements

### Logo Integration
- **Dashboard:** `fgt_dashboard/assets/logo.png`
  - Cropped logo (removed dead space: 1080x1080 → 1033x213)
  - Centered in header at 60px height
- **Check-in form:** `backend/static/logo.png`
  - Same cropped version
  - Positioned in footer at 320px width

---

## Check-in Form Updates (`backend/templates/checkin.html`)

- Enlarged "EVENT CHECK-IN" title (2.5rem, font-weight 700)
- Logo moved to footer (below submit button, above "Problems?" text)
- Fixed CSS `.logo` max-width that was blocking inline width styles

---

## Dashboard Fixes (`fgt_dashboard/callbacks.py`)

### STARTGG_TOKEN Fallback
```python
STARTGG_API_KEY = os.getenv("STARTGG_API_KEY") or os.getenv("STARTGG_TOKEN")
```
- Fixes issue where API key wasn't found (env file uses `STARTGG_TOKEN`)

### Tab Switching Callback
```python
@app.callback(
    Output("tab-checkins-content", "style"),
    Output("tab-settings-content", "style"),
    Input("tabs", "value"),
)
def switch_tabs(selected_tab):
    if selected_tab == "tab-settings":
        return {"display": "none"}, {"display": "block"}
    else:
        return {"display": "block"}, {"display": "none"}
```
- Enables switching between Check-ins and Settings tabs

### Simplified Airtable Update
- Changed `fetch_event_data()` to only save:
  - `active_event_slug`
  - `is_active`
- Removed redundant fields (`event_date`, `default_game`, `events_json`, etc.)
- These were causing 422 errors from Airtable

---

## Known Issue: n8n Workflow

The "FGC THN - Check-in Orchestrator" workflow needs manual configuration:

1. Open n8n at `http://localhost:5678`
2. Find "FGC THN - Check-in Orchestrator" workflow
3. Verify:
   - "Save to Airtable" node uses **"Airtable Latest"** credential
   - JavaScript code nodes have correct `$node` and `$json` references

### Expected JavaScript for Code Nodes

**Parse Input:**
```javascript
const webhookData = $node["Webhook"].json.body || $node["Webhook"].json;
const settings = $json || {};
const slug = settings.active_event_slug || null;
const pnr = webhookData.personnummer || null;
const tag = (webhookData.tag || "").trim();
const name = webhookData.namn || webhookData.name || null;
return [{ json: { slug, personnummer: pnr, tag, name, _runEbas: !!pnr, _runStartgg: !!tag && !!slug }}];
```

**Merge Results:**
```javascript
const ctx = $node["Parse Input"].json;
const ebas = $node["eBas Check"]?.json || { isMember: false };
const sgg = $node["Start.gg Check"]?.json || { isRegistered: false };
const missing = [];
const memberOk = ebas.isMember === true || !ctx._runEbas;
const startggOk = sgg.isRegistered === true || !ctx._runStartgg;
if (!memberOk) missing.push("Membership");
if (!startggOk) missing.push("Start.gg");
const ready = memberOk && startggOk;
return [{ json: { ready, status: ready ? "Ready" : "Pending", missing, member: ebas.isMember || false, startgg: sgg.isRegistered || false, name: ctx.name, tag: ctx.tag, slug: ctx.slug }}];
```

**Return Results:**
```javascript
const results = $node["Merge Results"].json;
return [{ json: results }];
```

---

## Files Changed

### Modified
- `fgt_dashboard/layout.py` - Dashboard redesign
- `fgt_dashboard/callbacks.py` - STARTGG_TOKEN fallback, tab switching, simplified Airtable update
- `backend/templates/checkin.html` - Logo in footer, larger title
- `backend/static/logo.png` - Replaced with cropped version

### New
- `fgt_dashboard/assets/logo.png` - Cropped logo for dashboard

---

## Pending Tasks

- [ ] Commit all changes
- [ ] Swish QR code (laptop) vs Swish link (mobile)
- [ ] Verify n8n workflow manually
