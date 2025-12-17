# 1. Arkitektur

Systemet är designat med en modern **microservices-inspirerad arkitektur**. Detta innebär att applikationen är uppdelad i flera mindre, oberoende tjänster som kommunicerar med varandra över ett nätverk. Alla tjänster körs i sina egna Docker-containrar, vilket gör systemet portabelt och lätt att driftsätta.

Här är en översikt över de primära komponenterna i systemet:

![Arkitekturdiagram](https://i.imgur.com/r8sH3jU.png)
*(Detta är ett förenklat diagram för att illustrera flödet mellan huvudkomponenterna)*

---

### Komponenter

#### 1. Backend (`backend/`)
*   **Teknik:** FastAPI (Python)
*   **Ansvar:** Detta är navet för den publika delen av applikationen.
    *   **Webbserver:** Serverar de HTML-sidor som användaren ser, t.ex. incheckningsformuläret (`checkin.html`) och statussidorna.
    *   **N8N Proxy:** All trafik från användarens webbläsare till `n8n` går via en proxy-endpoint (`/n8n/...`) i denna tjänst. Detta är en viktig säkerhetsfunktion som döljer den interna `n8n`-tjänsten. Proxyn fångar även upp inkommande data, validerar och "tvättar" den med hjälp av `validation.py` innan den skickas vidare.
    *   **Status API:** Tillhandahåller `GET /api/participant/{name}/status` som läser deltagarstatus direkt från Airtable. Detta endpoint används av `status_pending.html` för polling - se [Dataflöden](./2_Datafloden.md#flöde-3-status-polling-väntande-deltagare) för mer detaljer om varför detta designbeslut togs.

#### 2. FGT Dashboard (`fgt_dashboard/`)
*   **Teknik:** Plotly Dash monterad inuti en FastAPI-app.
*   **Ansvar:** Detta är turneringsorganisatörernas (TOs) primära verktyg.
    *   **Administrativt Gränssnitt:** Tillhandahåller ett webbgränssnitt (tillgängligt via `/admin/`) där TOs kan hantera och övervaka event.
    *   **Event-konfiguration:** Den mest kritiska funktionen är att hämta turneringsdata. En TO klistrar in en länk från Start.gg, och instrumentpanelen anropar Start.gg:s GraphQL API för att hämta alla relevanta detaljer (event, deltagare, etc.). Denna information sparas sedan i `settings`-tabellen i Airtable.
    *   **Realtidsöverblick:** Visar en live-uppdaterad tabell med alla incheckade deltagare och deras status (Grön, Röd, etc.). Den har också en "Needs Attention"-sektion för att snabbt identifiera vilka som behöver hjälp.

#### 3. N8N (`n8n/`)
*   **Teknik:** n8n.io (Workflow Automation)
*   **Ansvar:** Detta är systemets "hjärna" som utför själva incheckningslogiken.
    *   **Workflows:** `n8n` lyssnar på webhooks som anropas av `backend`-tjänsten.
    *   **Affärslogik:** När ett `checkin`-anrop kommer in, exekverar `n8n` ett workflow som utför följande steg:
        1.  Kontrollerar deltagarens medlemskap mot **Sverok eBas API**.
        2.  Kontrollerar deltagarens turneringsregistrering mot **Start.gg API** (baserat på `tag`).
        3.  Kontrollerar betalningsstatus.
        4.  Uppdaterar deltagarens rad i `active_event_data`-tabellen i Airtable med resultatet.
        5.  Returnerar ett `JSON`-svar till `backend` som indikerar om incheckningen lyckades eller vad som saknas.

#### 4. Nginx
*   **Teknik:** Nginx
*   **Ansvar:** Fungerar som en **reverse proxy** och systemets enda publika ingångspunkt.
    *   **Trafik-routing:** Tar emot all inkommande webbtrafik och dirigerar den till rätt intern tjänst baserat på URL:en.
        *   Anrop till `domän.se/admin/...` skickas till `fgt_dashboard`.
        *   Alla andra anrop (`domän.se/...`) skickas till `backend`.
    *   **SSL-terminering:** I produktionsmiljön hanterar Nginx HTTPS och SSL-certifikat (via Certbot) för att säkerställa krypterad trafik.

#### 5. Airtable
*   **Teknik:** Airtable (Cloud Database)
*   **Ansvar:** Fungerar som systemets centrala databas.
    *   **`settings`:** En tabell som innehåller konfigurationen för det nuvarande aktiva eventet. Denna data skrivs av `fgt_dashboard`.
    *   **`active_event_data`:** En tabell som fungerar som en live-databas för alla incheckningar under ett event. Denna data skrivs primärt av `n8n` och läses av både `backend` och `fgt_dashboard`.
