# 4. Externa Beroenden

Systemet är beroende av flera externa tjänster och API:er för att kunna fungera. En fungerande anslutning och korrekta API-nycklar för dessa tjänster är avgörande för systemets drift.

---

### 1. Airtable

*   **Roll:** Systemets centrala databas.
*   **Användning:**
    *   **Lagring av konfiguration:** `settings`-tabellen lagrar information om det aktiva eventet, som hämtas från Start.gg. Denna tabell skrivs till av **FGT Dashboard**.
    *   **Lagring av live-data:** `active_event_data`-tabellen innehåller en rad för varje deltagare som checkar in. Här sparas deras status (`Ready`, `Pending`), personlig information och vilka krav de uppfyller. Denna tabell skrivs primärt till av **N8N** och läses av både **Backend** och **FGT Dashboard**.
*   **Krav:**
    *   Ett Airtable-konto.
    *   En Airtable Base med de nödvändiga tabellerna (`settings`, `active_event_data`).
    *   `AIRTABLE_API_KEY`: En API-nyckel med behörighet att läsa och skriva till basen.
    *   `AIRTABLE_BASE_ID`: ID för den specifika basen som ska användas.
    *   Båda dessa värden måste konfigureras i `.env`-filen.

---

### 2. Start.gg

*   **Roll:** Källa för all turnerings- och deltagarinformation.
*   **Användning:**
    *   **Event-konfiguration:** **FGT Dashboard** använder Start.gg:s GraphQL API för att hämta detaljer om en turnering (namn, datum, ingående spelevent) när en TO klistrar in en turneringslänk.
    *   **Validering av deltagare:** **N8N**-flödet använder deltagarens gamer-tag för att anropa Start.gg:s API och verifiera att de är korrekt registrerade för det aktiva eventet.
*   **Krav:**
    *   Ett Start.gg-konto.
    *   `STARTGG_API_KEY`: En personlig API-nyckel för att kunna göra anrop mot GraphQL API:et. Denna måste konfigureras i `.env`-filen.
    *   För den administrativa OAuth-inloggningen krävs även `STARTGG_CLIENT_ID` och `STARTGG_CLIENT_SECRET`.

---

### 3. Sverok eBas

*   **Roll:** Källa för verifiering av medlemskap i den anslutna föreningen.
*   **Användning:**
    *   **Validering av medlemskap:** **N8N**-flödet anropar eBas API med en deltagares personnummer för att kontrollera om de är en aktiv, betalande medlem i föreningen.
    *   **Registrering av nya medlemmar:** Om en deltagare inte är medlem, kan det dynamiska registreringsformuläret använda eBas API för att registrera en ny medlem direkt.
*   **Krav:**
    *   Tillgång till föreningens API-nycklar för eBas.
    *   Dessa nycklar måste konfigureras som miljövariabler så att `n8n`-tjänsten kan komma åt dem.

---

### 4. One.com (Produktion)

*   **Roll:** DNS-hanterare för den publika domänen.
*   **Användning:**
    *   I produktionsmiljön pekar DNS-inställningarna för domänen `fgctrollhattan.se` (och subdomäner som `checkin.` och `admin.`) mot den server där Docker-containrarna körs. Detta gör att systemet blir tillgängligt över internet.
*   **Krav:**
    *   Ett konto hos One.com eller en annan DNS-leverantör där domänen är registrerad.
    *   Korrekt konfigurerade A-records eller CNAME-records.
