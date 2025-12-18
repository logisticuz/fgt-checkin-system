# Changelog: 2025-12-18 (Part 2)

Session fortsättning - fixar för check-in flödet, dashboard förbättringar och duplicat-hantering.

---

## Sammanfattning

Den här sessionen fokuserade på att:
1. **Fixa n8n workflow** - Check-in fungerade inte pga JavaScript-syntaxfel
2. **Implementera betalningsflöde** - TO måste manuellt godkänna betalning (Swish)
3. **Förhindra duplicerade check-ins** - Spelare ska kunna kolla sin status utan att skapa ny rad
4. **Förbättra TO-dashboard** - Reaktiva stats, kolumnväljare, radera-knapp

---

## n8n Workflow Fixes

### Problem
Check-in formuläret returnerade "Something went wrong" pga flera JavaScript-fel i workflow-noderna.

### Lösning
Fixade via Python-script som uppdaterar workflow via n8n REST API:

**Parse Input** - Fixade `$node["Webhook"]` referens:
```javascript
const webhookData = $node["Webhook"].json.body || $node["Webhook"].json;
const settings = $json.records?.[0]?.fields || {};
// ... resten av koden
```

**Merge Results** - Lade till UUID-generering och betalningslogik:
```javascript
// Generate UUID
const uuid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
  const r = Math.random() * 16 | 0;
  const v = c === 'x' ? r : (r & 0x3 | 0x8);
  return v.toString(16);
});

// Status är alltid Pending - TO måste verifiera betalning manuellt
return [{ json: {
  uuid,
  status: "Pending",
  payment_valid: false,
  // ... resten
}}];
```

**Save to Airtable** - Uppdaterade kolumnmappning för nya fält (UUID, payment_valid)

---

## Duplicat-kontroll

### Varför
Spelare kunde checka in flera gånger och skapa duplicerade rader. De vill kunna kolla sin status utan att skapa ny post.

### Lösning
Lade till tre nya noder i n8n workflow:

```
Webhook → Load Settings → Parse Input → Check Duplicate → IF Duplicate
  ├─ TRUE:  Already Checked In → Return Results (visar befintlig status)
  └─ FALSE: eBas + Start.gg → Wait → Merge → Save → Return (ny check-in)
```

**Check Duplicate** - Frågar Airtable:
```
filterByFormula: AND({tag}='spelarens-tag', {event_slug}='aktuellt-event')
```

**Already Checked In** - Returnerar befintlig status:
```javascript
const existing = $json.records[0].fields;
return [{
  json: {
    already_checked_in: true,
    status: existing.status || "Pending",
    payment_valid: existing.payment_valid || false,
    // ...
    message: "Already checked in"
  }
}];
```

---

## Dashboard Förbättringar

### Reaktiva Stats (`fgt_dashboard/callbacks.py`)
Stats-korten uppdateras nu automatiskt när tabelldata ändras:

```python
@app.callback(
    Output("stat-total", "children"),
    Output("stat-ready", "children"),
    Output("stat-pending", "children"),
    Output("stat-attention", "children"),
    Output("needs-attention-section", "style"),
    Input("checkins-table", "data"),
)
def update_stats(table_data):
    # Räknar Total, Ready, Pending, Need Attention
```

### TO Betalningsgodkännande
Klicka på `payment_valid`-cellen för att toggla:

```python
@app.callback(
    Output("payment-update-feedback", "children"),
    Output("checkins-table", "data", allow_duplicate=True),
    Input("checkins-table", "active_cell"),
    # ...
)
def approve_payment(active_cell, table_data, selected_slug):
    # Uppdaterar payment_valid i Airtable
    # Om payment_valid=True + member=True + startgg=True → status="Ready"
```

### Kolumnväljare i Settings (`fgt_dashboard/layout.py`)
TO kan välja vilka kolumner som visas i tabellen:

```python
dcc.Dropdown(
    id="column-visibility-dropdown",
    options=[
        {"label": "Name", "value": "name"},
        {"label": "Tag", "value": "tag"},
        {"label": "Telephone", "value": "telephone"},  # Behövs för Swish-verifikation
        # ...
    ],
    value=["name", "tag", "telephone", "member", "startgg", "payment_valid", "status"],
    multi=True,
)
```

### Radera-knapp
TO kan ta bort felaktiga/duplicerade poster:

```python
@app.callback(
    Output("delete-feedback", "children"),
    Output("checkins-table", "data", allow_duplicate=True),
    Input("btn-delete-selected", "n_clicks"),
    State("checkins-table", "active_cell"),
    State("checkins-table", "data"),
)
def delete_selected_player(n_clicks, active_cell, table_data):
    # Raderar från Airtable och uppdaterar tabell
```

---

## Airtable API Fix (`shared/airtable_api.py`)

Ändrade `id` till `record_id` för tydlighet:
```python
result.append({
    "record_id": r.get("id"),  # Airtable record ID för updates/deletes
    # ...
})
```

---

## Check-in Flöde (Nuvarande)

```
1. Spelare fyller i formulär (namn, tag, personnummer, telefon)
2. n8n workflow:
   a. Kollar om redan incheckad (tag + event_slug)
      - Om ja: Returnerar befintlig status
      - Om nej: Fortsätter...
   b. eBas Check (medlemskap via personnummer)
   c. Start.gg Check (registrerad på turneringen)
   d. Skapar ny rad i Airtable:
      - status = "Pending"
      - payment_valid = false
      - member = true/false
      - startgg = true/false
      - UUID genereras
3. Spelare ser resultat (vad som saknas)
4. TO ser spelaren i dashboard med gul "Pending" status
5. TO verifierar Swish-betalning manuellt (kollar telefonnummer)
6. TO klickar på payment_valid → true
7. Om alla checks OK → status = "Ready" (grön)
```

---

## Filer Ändrade

### Modifierade
- `fgt_dashboard/layout.py` - Kolumnväljare, radera-knapp, visible-columns-store
- `fgt_dashboard/callbacks.py` - Reaktiva stats, betalningsgodkännande, radera, kolumnfiltrering
- `shared/airtable_api.py` - `id` → `record_id`

### Nya (temporära script)
- `fix_workflow.py` - Uppdaterar n8n workflow med UUID/payment_valid
- `add_duplicate_check.py` - Lägger till duplicat-kontroll i workflow

---

## Pending Tasks

- [ ] Commit alla ändringar
- [ ] Ta bort temporära script (`fix_workflow.py`, `add_duplicate_check.py`)
- [ ] Testa full check-in flow från början till slut
- [ ] Swish-integration (ersätter manuell verifikation)
- [ ] Stripe-integration (alternativ betalning)
- [ ] Visa "Already checked in" meddelande tydligare i formuläret

---

## Kända Begränsningar

1. **Betalning är manuell** - TO måste kolla Swish och klicka godkänn
2. **Duplicat-check baseras på tag** - Om spelare byter tag kan de checka in igen
3. **Ingen bekräftelse vid radering** - TO kan råka radera fel spelare

---

## Tekniska Detaljer

### n8n Workflow ID
```
63F2NuQ8tqexEHEU
```

### n8n API
```bash
# Hämta workflow
curl -H "X-N8N-API-KEY: $API_KEY" http://localhost:5678/api/v1/workflows/63F2NuQ8tqexEHEU

# Uppdatera workflow
curl -X PUT -H "X-N8N-API-KEY: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"...", "nodes":[...], "connections":{...}}' \
  http://localhost:5678/api/v1/workflows/63F2NuQ8tqexEHEU
```

### Airtable Tabeller
- `settings` - Aktiv event config (active_event_slug, is_active)
- `active_event_data` - Check-ins för aktuellt event
