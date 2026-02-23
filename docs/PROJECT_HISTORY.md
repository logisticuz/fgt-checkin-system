# Projektets Historia: FGT Check-in System

> Från manuell incheckning till backend-orkestrerad mikroservicearkitektur med Postgres.
> Detta dokument förklarar inte bara *vad* som byggdes, utan *varför* varje beslut togs.

---

## Ursprunget: Problemet vi ville lösa

Före systemet var incheckning på FGC Trollhättans turneringar helt manuell. En TO (turneringsorganisatör) satt med en laptop och:
- Kollade om spelaren fanns i Start.gg
- Frågade om de var Sverok-medlem
- Tog emot Swish-betalning och prickade av manuellt
- Skrev in allt i ett kalkylblad

Med 15–50 spelare per event tog detta 30–60 minuter och var felbenäget. Idén var enkel: **låt spelarna checka in sig själva via en webbsida**, och låt systemet sköta verifieringen automatiskt.

---

## Fas 1: MVP — Airtable + n8n (före december 2025)

### Arkitekturen

```
Spelare → checkin.html → Backend (proxy) → n8n (hjärnan) → Airtable
                                                ├─ eBas API
                                                └─ Start.gg API
```

**Varför Airtable?**
- Snabbt att sätta upp — inget behov av att hantera databasinfrastruktur
- Inbyggd multi-select för spellistor
- Enkelt API för snabb prototyping
- Gratis på liten skala

**Varför n8n?**
- Visuell workflow-builder — lätt att iterera utan koddeployment
- Inbyggda noder för HTTP-anrop, Airtable, och logik
- Självhostning via Docker (ingen extern beroende)
- Perfekt för att koppla ihop externa API:er

**Vad backend gjorde:**
- Serverade HTML-sidor (checkin.html, register.html, etc.)
- Proxyade formulärdata till n8n (`/n8n/webhook/...`)
- Validerade och "tvättade" input (personnummer, telefon)

**Vad n8n gjorde (allt annat):**
- Laddade settings från Airtable
- Kollade dubbletter i Airtable
- Anropade eBas + Start.gg
- Skrev resultat till Airtable
- Beräknade status (Ready/Pending)
- Notifierade backend för SSE-broadcast

Det fungerade — men n8n var **systemets hjärna** och Airtable var **enda databasen**. Båda skulle visa sig bli flaskhalsar.

---

## Fas 2: Realtid och säkerhet (december 2025)

### SSE — 97% färre API-anrop (2025-12-18)

**Problemet:** Dashboarden pollade Airtable var 30:e sekund. Med 50 spelare under 2 timmar = ~1740 API-anrop per event. Airtable har en global gräns på ~5 req/s.

**Lösningen:** Server-Sent Events (SSE).
- Backend skapade en `SSEManager` som höll öppna anslutningar
- n8n skickade `POST /api/notify/checkin` efter varje incheckning
- Backend broadcastade till alla anslutna klienter i realtid
- **Resultat: ~50 anrop istället för 1740** — en 97% minskning

**Varför SSE istället för WebSockets?**
- Enklare att implementera (envägs-push räcker)
- HTTP/1.1-kompatibelt (ingen upgrade)
- Bättre för det vi behövde: server → klient-push

### Säkerhetshärdning (2025-12-19)

Sex säkerhetsförbättringar gjordes:
1. **Race condition:** Post-save duplikatkontroll (två samtida check-ins med samma tag)
2. **API-felhantering:** eBas/Start.gg-fel kraschar inte längre workflow
3. **Personuppgifter:** Personnummer tas bort från localStorage efter check-in
4. **Rate limiting:** Webhook-gräns (10r/m) striktare än generell trafik (30r/m)
5. **XSS-skydd:** `textContent` istället för `innerHTML` vid felvisning
6. **Validering:** Tom tag-validering tillagd

### Case-insensitivitet

En subtil men viktig fix: "Viktor" och "viktor" behandlades som olika spelare. All jämförelse gjordes case-insensitiv med `.toLowerCase()` i JavaScript och `LOWER()` i Airtable-queries.

---

## Fas 3: UX och konfigurerbara krav (december 2025 – januari 2026)

### Dashboard-evolution

Dashboarden gick från en enkel tabell till ett fullfjädrat TO-verktyg:
- **Ikoner istället för text:** ✓/✗ med färgkodning (grön/röd)
- **Stat-kort som filter:** Klickbara kort (Total, Ready, Pending, No Payment)
- **"Needs Attention":** Sektion som visar spelare som saknar något
- **Spelnamnsförkortningar:** "STREET FIGHTER 6" → "SF6"
- **Multi-delete:** Markera och radera flera spelare med bekräftelse

### Airtable API-konsolidering (2025-12-26)

**Problemet:** Airtable-anrop var utspridda i tre filer — `main.py`, `callbacks.py`, och `airtable_api.py`. Duplicerad kod, inkonsekvent felhantering.

**Lösningen:** Centralisera allt CRUD i `shared/airtable_api.py`. En abstraktion som alla delar av systemet använde.

**Lärdomen:** Ett lager av abstraktion framför externa API:er förhindrar underhållsproblem. Denna insikt la grunden för den senare storage-facaden.

### Konfigurerbara check-in-krav (2025-12-29)

**Problemet:** Alla events krävde samma saker — medlemskap, Start.gg, betalning. Men en casual weekly behöver kanske bara betalning, och ett gratis community-event behöver ingenting.

**Lösningen:** Tre konfigurerbara krav i settings-tabellen:

| Krav | Inställning | Default |
|------|------------|---------|
| Betalning | `require_payment` | On |
| Sverok-medlem | `require_membership` | On |
| Start.gg-registrering | `require_startgg` | On |

**Den envisa buggen:** Airtable-checkboxar returnerar `true` när de är bockade, men **fältet saknas helt** när de är obockade (inte `false`). Detta ledde till att:
```python
# Fel (require_payment är None, inte False):
if not require_payment: ...  # None är falsy → krav inaktiverat!

# Rätt:
if require_payment is not False: ...  # None → default On
```

Denna bugg fanns i både Python och n8n i månader och orsakade att spelare ibland fick fel status.

---

## Fas 4: Postgres-migrering (februari 2026)

### Varför byta från Airtable?

Fem konkreta problem drev beslutet:

1. **API Rate Limits:** ~5 req/s globalt. Med SSE löst, men fortfarande en risk vid skalning.
2. **Ingen atomär operation:** Dublettkontroll krävde läs-sedan-skriv (race condition).
3. **Latens:** Airtable hostad i USA, vi i Sverige.
4. **Kostnad:** Skalade avgifter vid större datamängder.
5. **Datamodelsbegränsningar:** Inga komplexa queries, foreign keys, eller transaktioner.

### Vad Postgres löste

| Problem | Airtable | Postgres |
|---------|----------|----------|
| Dublettkontroll | Läs → if not exists → skriv (race condition) | UPSERT ON CONFLICT (atomärt) |
| Arkivering | Manuell kopiering | Transaction: snapshot + stats + cleanup |
| Latens | ~200ms (US → SE) | <1ms (lokal Docker) |
| Kostnad | Skalande avgift | En Docker-container |
| Flexibilitet | Platta tabeller | JSONB, foreign keys, transaktioner |

### Storage-facaden — byt databas utan att röra resten

```python
# shared/storage.py
if DATA_BACKEND == "postgres":
    from shared.postgres_api import *
else:
    from shared.airtable_api import *
```

En enda miljövariabel (`DATA_BACKEND`) bestämmer vilken databas som används. Resten av koden (backend, dashboard) anropar samma funktioner oavsett. Airtable fungerar fortfarande som fallback.

### Arkivering och återöppning (2026-02-22)

Med Postgres på plats byggdes arkiveringspipelinen:
1. Ladda aktiva check-ins
2. Matcha/skapa spelarprofiler (via tag/email)
3. Skriv till `event_history` (replace mode — idempotent, ingen dubbelräkning)
4. Beräkna statistik → `event_stats`
5. Uppdatera `players` med eventdeltagande
6. Logga i `audit_log`

**Återöppning:** Om en TO arkiverat för tidigt kan eventet återöppnas från `event_history` — men bara om inga aktiva rader finns (skyddar mot att skriva över nya data).

---

## Fas 5: Backend-orkestrering (2026-02-23)

### Den stora arkitekturförändringen

**Före:** n8n var systemets hjärna — den laddade settings, kontrollerade dubbletter, anropade API:er, skrev till databasen, och beräknade status.

**Problem med det:**
- n8n hade 5–7 Airtable/databas-noder per workflow
- Frontenden anropade n8n direkt (via proxy)
- n8n var en **single point of failure**
- Byte av databas krävde omskrivning av workflows

**Efter:** Backend orkestrerar allt. n8n är en ren **integration engine**.

```
Före:
Form → Backend (proxy) → n8n (gör ALLT) → Airtable → n8n → Backend → SSE

Efter:
Form → Backend:
        ├─ Validera + sanitera
        ├─ Postgres UPSERT (atomär dedupe)
        ├─ Anropa n8n (ENBART externa API:er)
        ├─ Ta emot resultat via callback
        ├─ Beräkna status
        └─ SSE broadcast
```

**Varför denna separation?**

1. **Separation of concerns:** n8n gör det den är bra på (API-integrationer), backend gör det den är bra på (data, logik, HTTP)
2. **Atomär dedupe:** Postgres UPSERT eliminerar race conditions helt
3. **Graceful degradation:** Om n8n är nere skapas check-in ändå (Pending status). TO kan lösa manuellt.
4. **Inga Airtable-noder i workflows:** Rent snitt för Postgres-migrering
5. **Enkel rollback:** Bara 2 URL-ändringar för att gå tillbaka till gamla flödet

### Nya workflows

- **Checkin Orchestrator v5 (PG):** Noll Airtable-noder (v4 hade 5). Anropar Start.gg + eBas parallellt, rapporterar via `/api/integration/result`.
- **eBas Register v2 (PG):** Noll Airtable-noder (v1 hade 2). Rapporterar via `/api/checkin/{id}/member-status`.
- v4 och v1 deaktiverades (inte raderades — för säker rollback).

---

## Var vi är nu (2026-02-23)

### Arkitekturen idag

```
┌─────────────┐     ┌──────────────────────────────────────────┐
│   Spelare   │────→│  Backend (FastAPI)                        │
│ checkin.html │     │  ├─ Validering + sanitering              │
└─────────────┘     │  ├─ Postgres UPSERT (atomär dedupe)      │
                    │  ├─ Anropar n8n v5 (externa API:er)      │
                    │  ├─ Tar emot callbacks med resultat       │
                    │  ├─ Beräknar status (Ready/Pending)       │
                    │  └─ SSE broadcast till alla klienter      │
                    └──────────┬───────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │   Postgres   │  │     n8n      │  │  Dashboard   │
    │   (primär    │  │ (integration │  │  (Dash/SSE)  │
    │   databas)   │  │   engine)    │  │              │
    └──────────────┘  └──────┬───────┘  └──────────────┘
                             │
                    ┌────────┼────────┐
                    ▼                 ▼
            ┌─────────────┐  ┌──────────────┐
            │  Start.gg   │  │  Sverok eBas │
            │  (GraphQL)  │  │   (REST)     │
            └─────────────┘  └──────────────┘
```

### Status

| Komponent | DEV | PROD |
|-----------|-----|------|
| Postgres som primär DB | Klart | Pending deploy |
| Backend-orkestrering | Klart | Pending deploy |
| n8n v5/v2 (ren integration engine) | Klart | Pending deploy |
| Arkivering + återöppning | Klart | Pending deploy |
| Konfigurerbara krav | Klart | Klart |
| SSE realtid | Klart | Klart |

---

## Lärdomar längs vägen

### 1. SSE istället för polling
Även med bara 50 spelare sparade SSE 97% av API-anropen. Realtidsarkitektur lönar sig tidigt.

### 2. Atomära operationer löser race conditions
Airtable:s läs-sedan-skriv-mönster för dublettkontroll orsakade problem. Postgres UPSERT löste det med en rad SQL.

### 3. Separera concerns — n8n som integration engine
n8n var fantastisk som prototypverktyg, men blev en flaskhals när den ägde all logik. Att begränsa den till "anropa externa API:er och rapportera tillbaka" förenklade allt.

### 4. Abstraktionslager möjliggör migration
Storage-facaden (`shared/storage.py`) gjorde att vi kunde byta databas utan att röra backend eller dashboard. En miljövariabel räckte.

### 5. En källa för logik
Statusberäkningsbuggen (Airtable checkbox `None` vs `False`) fanns i både Python och n8n i månader. Ha logiken på **ett** ställe.

### 6. Idempotenta operationer
Arkivering med replace mode (radera gamla rader för samma slug innan insert) förhindrar dubbelräkning vid omarkivering.

### 7. Graceful degradation
Systemet skapar check-ins även om n8n är nere. TO:n kan lösa manuellt. Bättre än att hela flödet kraschar.

### 8. Case-sensitivity
Mänskligt inmatad data (tags, namn) måste normaliseras. `.toLowerCase()` överallt förhindrar att "Viktor" och "viktor" blir två spelare.

### 9. Konfiguration framför kod
Konfigurerbara krav eliminerade behovet av olika versioner för olika eventtyper. En settings-rad styr allt.

### 10. Separera status från data
`memberCheckPassed` (för statusberäkning) ≠ `isActualMember` (för lagring). Att blanda ihop dessa gav fel medlemsstatus i databasen.
