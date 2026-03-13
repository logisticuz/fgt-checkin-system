# 6. API Referens

Detta dokument beskriver de API-endpoints som systemet exponerar. Systemet är uppdelat i en `backend`-tjänst som hanterar incheckning och status, och en `fgt_dashboard`-tjänst för administration.

---

## 1. Backend API (`backend/main.py`)

Dessa endpoints är tillgängliga via `backend`-tjänsten.

### 1.1 Incheckning & Status

#### `POST /api/checkin/orchestrate`
*   **Beskrivning:** Huvudsaklig endpoint för deltagare att checka in. Backend orkestrerar hela flödet: validering, Postgres UPSERT (deduplikering), anrop till n8n v5 för externa kontroller (Start.gg + eBas), statusberäkning, och SSE-broadcast.
*   **Metod:** `POST`
*   **Request Body (JSON):**
    ```json
    {
      "namn": "Deltagarens Fulla Namn",
      "telefon": "0701234567",
      "tag": "PlayerTag123",
      "personnummer": "YYYYMMDDXXXX",
      "acquisition_source": "discord"
    }
    ```
*   **Noteringar:**
    *   `acquisition_source` är valfritt och används när `settings.collect_acquisition_source=true`.
    *   Backend sätter `added_via="startgg_flow"` automatiskt för detta flöde.
*   **Validering (på servern):**
    *   Payloaden valideras av `backend/validation.py`.
    *   Fält saneras (t.ex. `personnummer` normaliseras till bara siffror).
    *   Om valideringen misslyckas returneras `HTTP 400` med en lista av fel.
*   **Svar (JSON):**
    *   **Om deltagaren redan är incheckad:**
        ```json
        {
          "already_checked_in": true,
          "status": "Ready",
          // ...andra statusfält
        }
        ```
    *   **Om ny incheckning:**
        ```json
        {
          "ready": false,
          "status": "Pending",
          "missing": ["Payment"],
          // ...andra statusfält
        }
        ```

#### `POST /api/ebas/register`
*   **Beskrivning:** Registrerar en ny Sverok-medlem via n8n eBas Register v2. Resultatet rapporteras tillbaka asynkront via `/api/checkin/{id}/member-status`.
*   **Metod:** `POST`
*   **Request Body (JSON):**
    ```json
    {
      "personnummer": "YYYYMMDDXXXX",
      "checkin_id": "...",
      "name": "Deltagarens Namn"
    }
    ```

#### `GET /api/participant/{name}/status`
*   **Beskrivning:** Hämtar en deltagares aktuella incheckningsstatus från Postgres. Används av `status_pending.html` för att polla efter uppdateringar (t.ex. efter att en TO manuellt godkänt en betalning).
*   **Metod:** `GET`
*   **URL-parametrar:**
    *   `name` (str): Deltagarens namn eller tag.
*   **Svar (JSON):**
    ```json
    {
      "ready": true,
      "status": "Ready",
      "missing": [],
      "member": true,
      "payment": true,
      "startgg": true,
      "name": "Deltagarens Namn",
      "tag": "PlayerTag123",
      "startgg_events": ["Street Fighter 6"],
      "payment_expected": 100,
      "require_payment": true,
      "require_membership": true,
      "require_startgg": false
    }
    ```

#### `PATCH /api/player/games`
*   **Beskrivning:** Används när en spelare manuellt väljer vilka spel de ska delta i (om de t.ex. inte hittades på Start.gg).
*   **Metod:** `PATCH`
*   **Request Body (JSON):**
    ```json
    {
      "tag": "PlayerTag123",
      "slug": "tournament-slug",
      "games": ["Street Fighter 6", "Tekken 8"]
    }
    ```
*   **Svar (JSON):**
    ```json
    {
      "success": true,
      "tag": "PlayerTag123",
      "games": ["Street Fighter 6", "Tekken 8"]
    }
    ```

#### `PATCH /api/player/member`
*   **Beskrivning:** Uppdaterar en spelares medlemsstatus manuellt.
*   **Metod:** `PATCH`

### 1.2 Dashboard & Administration

#### `PATCH /players/{record_id}/payment`
*   **Beskrivning:** Används av TO-dashboarden för att manuellt markera en spelares betalning som godkänd eller icke-godkänd. **Triggar ett SSE-event** via `/api/notify/update` för att omedelbart uppdatera anslutna klienter (som spelarens statussida).
*   **Metod:** `PATCH`
*   **URL-parametrar:**
    *   `record_id` (str): Postgres record ID för spelaren.
*   **Request Body (JSON):**
    ```json
    { "payment_valid": true }
    ```
*   **Svar (JSON):**
    ```json
    {
      "success": true,
      "record_id": "...",
      "payment_valid": true
    }
    ```

#### `GET /players`
*   **Beskrivning:** Hämtar en lista på alla spelare via `shared.storage` (Postgres eller Airtable beroende på `DATA_BACKEND`).
*   **Metod:** `GET`

#### `GET /event-history`
*   **Beskrivning:** Hämtar historiska eventdata via `shared.storage`.
*   **Metod:** `GET`

### 1.3 Admin-verktyg

#### `POST /api/admin/recheck-startgg`
*   **Beskrivning:** Kör om Start.gg-kontrollen för en enskild spelare. Uppdaterar Start.gg-status, registrerade event, och email i Postgres.
*   **Metod:** `POST`

#### `POST /api/admin/bulk-recheck-startgg`
*   **Beskrivning:** Kör om Start.gg-kontrollen för **alla** spelare i det aktiva eventet. Loopar alla spelare med tag, anropar n8n Start.gg Check för varje, applicerar resultat (email, events, startgg-flagga). 0.3s delay mellan anrop för att respektera Start.gg rate limits.
*   **Metod:** `POST`
*   **Svar (JSON):**
    ```json
    {
      "total": 34,
      "checked": 34,
      "emails_found": 29,
      "errors": 0
    }
    ```

#### `POST /api/startgg/registered-count`
*   **Beskrivning:** Tar emot antal registrerade spelare från Start.gg (via n8n eller dashboard) och uppdaterar `events_json.tournament_entrants` i aktiva inställningar. Används för no-show-beräkning vid arkivering.
*   **Metod:** `POST`

### 1.4 Event-livscykel (Arkivering)

#### `POST /api/archive/event`
*   **Beskrivning:** Arkiverar det aktiva eventet. Flyttar alla check-in-rader till `event_archive`, beräknar statistik (inklusive no-show-metrik) och sparar i `event_stats`. Rensar `active_event_data`.
*   **Notering:** Archive-flödet kör även soft integrity-kontroller och loggar varningar vid mismatch (utan att blockera arkivering).
*   **Metod:** `POST`

#### `POST /api/archive/reopen`
*   **Beskrivning:** Återöppnar ett arkiverat event. Återställer check-in-data från `event_archive` till `active_event_data` (inklusive `player_uuid`). Rensar stale `startgg_event_url` och `events_json` i settings så att TO kan hämta färsk Start.gg-data.
*   **Metod:** `POST`

#### `POST /api/archive/delete`
*   **Beskrivning:** Permanent radering av ett arkiverat event (kräver explicit bekräftelse).
*   **Metod:** `POST`

### 1.5 Integration Engine (n8n/external)

Dessa endpoints är avsedda för integrationslager (n8n) där backend/Postgres är source of truth.

#### `POST /api/checkin/begin`
*   **Beskrivning:** Startar eller uppdaterar ett checkin-försök och returnerar `checkin_id`. Använder Postgres UPSERT.
*   **Metod:** `POST`
*   **Request Body (JSON):**
    ```json
    {
      "event_slug": "fight-night-17",
      "payload": {
        "name": "Player Name",
        "tag": "PlayerTag",
        "telephone": "0701234567",
        "email": "player@example.com",
        "added_via": "api",
        "acquisition_source": "friend"
      }
    }
    ```
*   **Noteringar:**
    *   `payload.added_via` är valfritt. Tillåtna värden: `manual_dashboard`, `startgg_flow`, `api`, `reopen_restore`, `unknown`.
    *   Om `added_via` saknas i request sätter backend default till `api`.
    *   `payload.acquisition_source` normaliseras till tillåtna källor (`friend`, `discord`, `startgg`, `social`, `venue`, `other`) eller ignoreras.
*   **Svar (JSON):**
    ```json
    {
      "success": true,
      "checkin_id": "...",
      "record_id": "...",
      "event_slug": "fight-night-17",
      "created": true
    }
    ```

#### `POST /api/integration/result`
*   **Beskrivning:** Applicerar resultat från en extern integration (t.ex. `startgg`, `ebas`) på ett checkin. Uppdaterar Postgres med resultat, beräknar status, triggar SSE-broadcast, och loggar audit-händelse. För `startgg`-källa sparas även `email` om tillgänglig.
*   **Metod:** `POST`
*   **Request Body (JSON):**
    ```json
    {
      "checkin_id": "...",
      "source": "startgg",
      "ok": true,
      "data": {
        "registered": true,
        "startgg_event_id": "123456",
        "email": "player@example.com"
      },
      "error": null,
      "fetched_at": "2026-02-22T14:30:00Z"
    }
    ```

#### `POST /api/checkin/{checkin_id}/member-status`
*   **Beskrivning:** Endpoint för eBas-registreringsflöde som sätter `member` direkt för ett checkin. Anropas av n8n eBas Register v2.
*   **Metod:** `POST`
*   **Request Body (JSON):**
    ```json
    { "member": true }
    ```

### 1.6 Server-Sent Events (SSE) for Realtidsuppdateringar

Dessa endpoints utgor ryggraden i realtidsfunktionaliteten for dashboarden.

#### `GET /api/events/stream`
*   **Beskrivning:** En klient (dashboarden eller status_pending.html) ansluter till denna endpoint for att prenumerera pa handelser. Anslutningen halls oppen.
*   **Metod:** `GET`
*   **Svar:** En `text/event-stream` strom som skickar handelser. Exempel:
    ```
    event: checkin
    data: {"type": "new_checkin", "name": "Ny Spelare", ...}

    : keepalive
    ```

#### `POST /api/notify/checkin` och `POST /api/notify/update`
*   **Beskrivning:** Webhooks som triggar SSE-broadcasts. Anropas av backend internt efter databasuppdateringar, eller av n8n efter externa operationer.
*   **Metod:** `POST`
*   **Request Body (JSON):** Flexibel, innehaller data som ska sandas.

### 1.7 OAuth (Start.gg)

#### `GET /login`
*   **Beskrivning:** Initierar Start.gg OAuth-inloggningsflode for admin-dashboard.

#### `GET /auth/callback`
*   **Beskrivning:** OAuth callback fran Start.gg. Visar en bridge page under token-utbyte, sedan redirect till dashboard.

### 1.8 System & Halsa

#### `GET /health`
*   **Beskrivning:** En lattviktig halsocheck som verifierar integration engine enligt `INTEGRATION_ENGINE` (standard `n8n`). Returnerar metadata om `data_backend` och integration engine.
*   **Metod:** `GET`

#### `GET /health/deep`
*   **Beskrivning:** En djupare halsocheck som verifierar data-backend (`postgres` eller `airtable`) samt integration engine. Ska endast anvandas for manuell felsokning.
*   **Metod:** `GET`

---

## 2. Autentisering och Sakerhet

*   **Start.gg OAuth:** Admin-dashboard anvander Start.gg OAuth for inloggning (prod). Dev-miljo har ingen auth.
*   **N8N Webhook Token:** Om `N8N_WEBHOOK_TOKEN` ar satt i `.env`, maste anrop fran backend till n8n inkludera denna token.
*   **Server-side Validering:** All inkommande data till `POST /api/checkin/orchestrate` valideras och saneras pa servern innan den processas, som ett skydd mot felaktig eller skadlig data.
*   **Integrationsmodell:** n8n fungerar som integrationslager (Start.gg/eBas), medan backend/Postgres ager datamodell, checkin-state och audit-logik.
*   **Rate Limiting:** Nginx tillampardistinction rate limits: 30 req/min generell trafik, 10 req/min for webhooks.
*   **Basic Auth:** I prod-miljo skyddas admin-dashboard av basic auth via nginx (utover OAuth).
