# 4. Externa Beroenden

Systemet är beroende av flera externa tjänster och API:er för att kunna fungera. En fungerande anslutning och korrekta API-nycklar för dessa tjänster är avgörande för systemets drift.

---

### 1. PostgreSQL

*   **Roll:** Systemets **primära databas**.
*   **Användning:**
    *   **Lagring av konfiguration:** `settings`-tabellen lagrar information om det aktiva eventet, som hämtas från Start.gg. Denna tabell skrivs till av **FGT Dashboard** och läses av **Backend**.
    *   **Lagring av live-data:** `active_event_data`-tabellen innehåller en rad för varje deltagare som checkar in. Här sparas deras status (`Ready`, `Pending`), personlig information och vilka krav de uppfyller. Denna tabell skrivs till av **Backend** (via orchestrate-flödet) och läses av **FGT Dashboard**.
    *   **Arkivering:** `event_history`-tabellen lagrar arkiverade event-data. `event_stats` lagrar aggregerad statistik.
    *   **Spelare och audit:** `players`-tabellen hanterar spelarprofiler över tid. `audit_log` loggar viktiga systemhändelser.
    *   **Deduplikering:** Check-in-flödet använder UPSERT (ON CONFLICT) för atomär deduplicering — ingen race condition möjlig.
*   **Krav:**
    *   `DATABASE_URL` — Postgres-anslutningssträng (konfigureras i `.env`).
    *   `POSTGRES_PASSWORD` — Lösenord för Postgres-användaren.
    *   Postgres körs som en Docker-container i stacken (`docker-compose.dev.yml` / `docker-compose.prod.yml`).

---

### 2. Airtable (Legacy Fallback)

*   **Roll:** Tidigare primär databas, nu tillgänglig som **fallback** om `DATA_BACKEND=airtable` sätts i `.env`.
*   **Användning:**
    *   Samma tabellschema (`settings`, `active_event_data`, etc.) stöds fortfarande via `shared/airtable_api.py`.
    *   Storage-facaden (`shared/storage.py`) väljer automatiskt rätt backend baserat på `DATA_BACKEND`.
*   **Krav (om aktiverad):**
    *   `DATA_BACKEND=airtable` i `.env`.
    *   `AIRTABLE_API_KEY` och `AIRTABLE_BASE_ID` från ditt Airtable-konto.

---

### 3. Start.gg

*   **Roll:** Källa för all turnerings- och deltagarinformation.
*   **Användning:**
    *   **Event-konfiguration:** **FGT Dashboard** använder Start.gg:s GraphQL API för att hämta detaljer om en turnering (namn, datum, ingående spelevent) när en TO klistrar in en turneringslänk.
    *   **Validering av deltagare:** **Backend** orkestrerar check-in-flödet och anropar **n8n v5** som i sin tur anropar Start.gg:s API via sub-workflowet `Start.gg Check` för att verifiera att deltagaren är registrerad.
*   **Krav:**
    *   `STARTGG_API_KEY` eller `STARTGG_TOKEN` — API-nyckel för GraphQL-anrop (konfigureras i `.env`).
    *   För den administrativa OAuth-inloggningen krävs även `STARTGG_CLIENT_ID` och `STARTGG_CLIENT_SECRET`.

---

### 4. Sverok eBas

*   **Roll:** Källa för verifiering av medlemskap i den anslutna föreningen.
*   **Användning:**
    *   **Validering av medlemskap:** **Backend** orkestrerar check-in-flödet och anropar **n8n v5** som i sin tur anropar eBas API via sub-workflowet `eBas Membership Check` med en deltagares personnummer.
    *   **Registrering av nya medlemmar:** Om en deltagare inte är medlem, kan registreringsformuläret använda eBas API via **n8n eBas Register v2** för att registrera en ny medlem direkt. Resultatet rapporteras tillbaka till backend via `/api/checkin/{id}/member-status`.
*   **Krav:**
    *   Tillgång till föreningens API-nycklar för eBas (konfigureras som miljövariabler i n8n).

---

### 5. One.com (Produktion)

*   **Roll:** DNS-hanterare för den publika domänen.
*   **Användning:**
    *   I produktionsmiljön pekar DNS-inställningarna för domänen `fgctrollhattan.se` (och subdomäner som `checkin.` och `admin.`) mot den server där Docker-containrarna körs. Detta gör att systemet blir tillgängligt över internet.
*   **Krav:**
    *   Ett konto hos One.com eller en annan DNS-leverantör där domänen är registrerad.
    *   Korrekt konfigurerade A-records för `checkin.fgctrollhattan.se` och `admin.fgctrollhattan.se`.
