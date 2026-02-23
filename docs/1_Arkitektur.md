# 1. Arkitektur

Systemet är designat med en modern **microservices-inspirerad arkitektur**. Detta innebär att applikationen är uppdelad i flera mindre, oberoende tjänster som kommunicerar med varandra över ett nätverk. Alla tjänster körs i sina egna Docker-containrar, vilket gör systemet portabelt och lätt att driftsätta.

Här är en översikt över de primära komponenterna i systemet:

![Arkitekturdiagram](https://i.imgur.com/r8sH3jU.png)
*(Detta är ett förenklat diagram för att illustrera flödet mellan huvudkomponenterna)*

---

### Komponenter

#### 1. Backend (`backend/`)
*   **Teknik:** FastAPI (Python)
*   **Ansvar:** Detta är navet för den publika delen av applikationen, orchestrering av check-in-flödet, och realtidsdataflödet.
    *   **Webbserver:** Serverar de HTML-sidor som användaren ser, t.ex. incheckningsformuläret (`checkin.html`) och statussidorna.
    *   **Orchestrering:** Huvudendpointen `POST /api/checkin/orchestrate` tar emot formulärdata, validerar och "tvättar" den (via `validation.py`), skapar/uppdaterar en incheckning i Postgres (UPSERT med dedupliceringskontroll), och anropar sedan n8n för externa kontroller (Start.gg, eBas). När n8n rapporterar tillbaka beräknar backend slutstatus och skickar SSE-broadcast.
    *   **Status API:** Tillhandahåller `GET /api/participant/{name}/status` som läser deltagarstatus direkt från Postgres. Detta endpoint används av `status_pending.html` för polling.
    *   **SSE Hub:** Hanterar Server-Sent Events (`GET /api/events/stream`) för realtidsuppdateringar till dashboarden. Exponerar även `/api/notify/checkin` och `/api/notify/update` som triggar SSE-broadcasts. Backend fungerar som bryggan för realtidsflöden mellan n8n-callbacks och klienterna.
    *   **Integrations-callbacks:** Tar emot resultat från n8n via `POST /api/integration/result` (Start.gg/eBas-status) och `POST /api/checkin/{id}/member-status` (eBas-registrering). Backend äger all data: den skriver till Postgres, beräknar status, och broadcastar via SSE.

#### 2. FGT Dashboard (`fgt_dashboard/`)
*   **Teknik:** Plotly Dash monterad inuti en FastAPI-app.
*   **Ansvar:** Detta är turneringsorganisatörernas (TOs) primära verktyg.
    *   **Administrativt Gränssnitt:** Tillhandahåller ett webbgränssnitt (tillgängligt via `/admin/`) där TOs kan hantera och övervaka event.
    *   **Event-konfiguration:** En TO klistrar in en länk från Start.gg, och instrumentpanelen anropar Start.gg:s GraphQL API för att hämta alla relevanta detaljer (event, deltagare, etc.). Denna information sparas sedan i `settings`-tabellen i Postgres.
    *   **Realtidsöverblick:** Visar en live-uppdaterad tabell med alla incheckade deltagare och deras status (Ready, Pending, etc.), mottagen via **Server-Sent Events (SSE)**. Den har också en "Needs Attention"-sektion för att snabbt identifiera vilka som behöver hjälp.
    *   **Arkivering:** TOs kan arkivera avslutade events till `event_history`-tabellen, samt återöppna arkiverade events vid behov.

#### 3. N8N (`n8n/`)
*   **Teknik:** n8n.io (Workflow Automation)
*   **Ansvar:** N8N fungerar som en **integration engine** som anropar externa API:er och rapporterar tillbaka till backend. Den äger ingen data och skriver inte till databasen.
    *   **Aktiva Workflows:**
        *   **Checkin Orchestrator v5 (PG)** (`checkin/validate-v5`) — Tar emot check-in-data från backend, anropar Start.gg och eBas parallellt via sub-workflows, och rapporterar resultaten tillbaka till backend via `POST /api/integration/result`.
        *   **eBas Register v2 (PG)** (`ebas/register-v2`) — Hanterar eBas-medlemsregistrering och rapporterar tillbaka via `POST /api/checkin/{id}/member-status`.
    *   **Sub-workflows:** `eBas Membership Check` och `Start.gg Check` anropas internt av v5-orkestratorn.
    *   **Designprincip:** n8n gör INGA databasskrivningar. Den anropar enbart externa API:er (Sverok eBas, Start.gg) och rapporterar resultaten till backend, som äger all datapersistens och statusberäkning.

#### 4. Nginx
*   **Teknik:** Nginx
*   **Ansvar:** Fungerar som en **reverse proxy** och systemets enda publika ingångspunkt.
    *   **Trafik-routing:** Tar emot all inkommande webbtrafik och dirigerar den till rätt intern tjänst baserat på URL:en.
        *   Anrop till `admin.fgctrollhattan.se` (prod) eller `localhost:8088/admin/` (dev) skickas till `fgt_dashboard`.
        *   Alla andra anrop skickas till `backend`.
    *   **SSL-terminering:** I produktionsmiljön hanterar Nginx HTTPS och SSL-certifikat (via Certbot) för att säkerställa krypterad trafik.
    *   **Rate limiting:** 30 req/min generell trafik, 10 req/min för webhooks.

#### 5. Postgres
*   **Teknik:** PostgreSQL
*   **Ansvar:** Systemets **primära databas**. All checkin-data, eventinställningar, arkivering och audit-loggar lagras här.
    *   **Tabeller:** `active_event_data`, `settings`, `event_history`, `event_stats`, `players`, `audit_log`.
    *   **Deduplikering:** Check-in-flödet använder Postgres UPSERT (ON CONFLICT) för atomär dedupliceringskontroll — inga race conditions.
    *   **Storage facade:** `shared/storage.py` abstraherar databasbackend och kan växla mellan Postgres (`shared/postgres_api.py`) och Airtable (`shared/airtable_api.py`) via miljövariabeln `DATA_BACKEND`.

#### 6. Airtable (Legacy Fallback)
*   **Teknik:** Airtable (Cloud Database)
*   **Ansvar:** Tidigare primär databas, nu tillgänglig som **fallback** om `DATA_BACKEND=airtable` sätts i `.env`. Samma tabellschema (`settings`, `active_event_data`, etc.) stöds fortfarande via `shared/airtable_api.py`, men Postgres är standard och rekommenderat.
