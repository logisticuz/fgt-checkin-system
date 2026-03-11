# 2. Dataflöden

Det finns två huvudsakliga dataflöden i systemet: ett för administratören (TO) som konfigurerar ett event, och ett för deltagaren som checkar in.

---

### Flöde 1: Admin - Konfiguration av Event

Detta flöde beskriver hur en turneringsorganisatör (TO) förbereder systemet för ett nytt event. All interaktion sker via **FGT Dashboard**.

1.  **Öppna Instrumentpanelen:** TO navigerar till `https://admin.fgctrollhattan.se` (eller `http://localhost:8088/admin/` i utvecklingsmiljö).
2.  **Klistra in Länk:** I "Settings"-fliken klistrar TO in den fullständiga URL:en till den specifika turneringen på Start.gg.
3.  **Hämta Eventdata:** TO klickar på knappen "Fetch Event Data".
4.  **Anrop till Start.gg:**
    *   `fgt_dashboard`-tjänsten tar emot anropet.
    *   Den extraherar turnerings-sluggen från URL:en.
    *   Den skickar en GraphQL-förfrågan till **Start.gg:s API** för att hämta turneringsinformation, inklusive namn, datum och en lista över alla spelevent som ingår.
5.  **Uppdatera Postgres:**
    *   `fgt_dashboard` anropar storage-facaden (`shared/storage.py`) som skriver till Postgres.
    *   Den hittar den "aktiva" raden i `settings`-tabellen (där `is_active = TRUE`).
    *   Den uppdaterar raden med den hämtade informationen: turnerings-slug, namn, datum, och en JSON-lista över alla spelevent.
6.  **Bekräftelse:** Instrumentpanelen visar ett meddelande som bekräftar att inställningarna har uppdaterats. Systemet är nu konfigurerat och redo att ta emot incheckningar för det specifika eventet.

---

### Flöde 2: Deltagare - Incheckning

Detta flöde beskriver vad som händer från det att en deltagare öppnar incheckningssidan till att statusen visas. **Backend orkestrerar hela flödet** — n8n används enbart som integration engine för externa API-anrop.

1.  **Öppna Incheckningssidan:** Deltagaren navigerar till `https://checkin.fgctrollhattan.se` (eller `http://localhost:8088/` i utvecklingsmiljö). `Backend`-tjänsten serverar `checkin.html`.
2.  **Fylla i Formulär:** Deltagaren fyller i sitt namn, telefonnummer, personnummer och **tag**.
3.  **Validering (Frontend):** När deltagaren klickar på "Check In", körs `validation.js` i webbläsaren:
    *   Grundläggande kontroller utförs (t.ex. att fält inte är tomma, att personnummer har rätt längd).
    *   Om fel upptäcks, visas ett felmeddelande direkt på sidan och formuläret skickas inte.
4.  **Anrop till Backend Orchestrator:**
    *   Om frontend-valideringen passerar, skickas datan som en `POST`-förfrågan i `JSON`-format till `POST /api/checkin/orchestrate`.
5.  **Validering (Backend):** `Backend`-tjänsten tar emot anropet.
    *   Den använder `validation.py` för att utföra samma validering igen på serversidan (som en säkerhetsåtgärd).
    *   Den "tvättar" datan, t.ex. genom att ta bort bindestreck och mellanslag från personnummer och telefonnummer.
6.  **Skapa Incheckning i Postgres:** Backend skriver direkt till Postgres via `begin_checkin()`. Denna funktion använder UPSERT (ON CONFLICT) för atomär dedupliceringskontroll — ingen race condition möjlig.
7.  **Anropa n8n:** Backend anropar n8n v5-orkestratorn (`checkin/validate-v5`) med check-in-data och checkin-ID.
8.  **n8n utför externa kontroller:** n8n anropar Start.gg och eBas parallellt via sub-workflows. n8n skriver **inte** till databasen.
9.  **n8n rapporterar tillbaka:** n8n skickar resultaten (per källa) till backend via `POST /api/integration/result`. Backend tar emot, uppdaterar Postgres med resultat (member, startgg, etc.), och beräknar slutstatus.
10. **SSE Broadcast:** Backend skickar en SSE-broadcast med uppdaterad status till alla anslutna klienter (dashboard + status-sidor).
11. **Svar till Frontend:** Backend returnerar ett JSON-svar till formuläret med status och eventuella saknade krav.

12. **Omdirigering (Redirect):** JavaScript-koden i `checkin.html` tar emot svaret och agerar baserat på innehållet:
    *   **Om `ready` är `true`:** Användaren omdirigeras till sin personliga statussida (`status_ready.html`), där de ser en bekräftelse på att allt är klart.
    *   **Om `ready` är `false`:** Användaren omdirigeras till registreringssidan (`register.html`) med query-parametrar som indikerar vad som saknas. Denna sida visar dynamiskt de formulär som krävs, inklusive en Swish-integration med QR-kod (desktop) eller deep link (mobil) om betalning saknas.

### Logik för 'Ready'-status

För att en deltagare ska få statusen `Ready` kontrolleras kraven mot eventets inställningar i `settings`-tabellen. Kraven är **konfigurerbara per event**:

| Krav | Setting | Default |
|------|---------|---------|
| Betalt | `require_payment` | On |
| Sverok-medlem | `require_membership` | On |
| Start.gg-registrerad | `require_startgg` | On |

Logiken (i `backend/main.py` och `fgt_dashboard/callbacks.py`):
```python
is_ready = (
    (not require_payment or payment_valid) and
    (not require_membership or member) and
    (not require_startgg or startgg)
)
```

Om en TO t.ex. stänger av `require_startgg` för en casual weekly, blir spelare `Ready` utan Start.gg-registrering.

---

### Djupdykning: N8N-arbetsflöden

N8N fungerar som en **integration engine** — den anropar externa API:er och rapporterar resultat tillbaka till backend. Den äger ingen data och skriver inte till databasen.

#### Checkin Orchestrator v5 (PG) — `checkin/validate-v5`

Det primära arbetsflödet för check-in-verifiering.

*   **Trigger:** Webhook (POST) — anropas av backend med check-in-data och checkin-ID.
*   **Parallella kontroller:** Anropar eBas Membership Check och Start.gg Check som sub-workflows parallellt.
*   **Felhantering:** `onError: continueRegularOutput` / `continueOnFail: true` — om ett externt API är nere rapporteras det som ett partiellt resultat istället för att hela flödet fallerar.
*   **Rapportera tillbaka:** Skickar resultaten (per källa: `startgg`, `ebas`) till backend via `POST /api/integration/result`.
*   **Notera:** v5 innehåller inga Airtable-noder. All datapersistens hanteras av backend.

#### eBas Register v2 (PG) — `ebas/register-v2`

Hanterar registrering av nya Sverok-medlemmar.

*   **Trigger:** Webhook (POST) — anropas av backend med personnummer och medlemsdata.
*   **Registrering:** Anropar Sveroks eBas API för att registrera en ny medlem.
*   **Rapportera tillbaka:** Skickar resultatet till backend via `POST /api/checkin/{id}/member-status`.
*   **Notera:** v2 innehåller inga Airtable-noder.

#### Sub-workflows (oförändrade)

*   **eBas Membership Check** — Anropar eBas `confirm_membership` med personnummer. Returnerar `isMember` (true/false).
*   **Start.gg Check** — Anropar Start.gg GraphQL med tag och slug. Returnerar `isRegistered` (true/false), matchade events, och `email` (om tillgänglig från Start.gg).

---

### Flöde 3: Realtidsuppdatering (Server-Sent Events)

Systemet använder Server-Sent Events (SSE) för att skicka omedelbara uppdateringar till klienter (både FGT Dashboard och deltagarens statussida) utan behov av kontinuerlig polling.

1.  **Klient Ansluter:** När **FGT Dashboard** eller en deltagares `status_pending.html`-sida laddas, ansluter en JavaScript-klient (`sse-client.js`) till `GET /api/events/stream` på `Backend`-tjänsten. Detta håller en öppen anslutning.
2.  **Händelse Sker:** En händelse som kräver en uppdatering inträffar. Det finns två huvudtyper:
    *   **Ny incheckning eller integrationsresultat:** Backend tar emot data (via orchestrate-endpointen eller n8n-callbacks), uppdaterar Postgres, och triggar en SSE-broadcast.
    *   **Manuell uppdatering:** En TO klickar på en knapp i dashboarden (t.ex. godkänner en betalning). Dashboarden anropar en API-endpoint (t.ex. `PATCH /players/{id}/payment`), som i sin tur triggar en SSE-broadcast.
3.  **Backend Broadcast:** `Backend`-tjänstens `SSEManager` skickar omedelbart ett data-paket över alla öppna anslutningar.
4.  **Klient Agerar:** JavaScript-klienten på respektive sida tar emot eventet:
    *   **Dashboard:** Triggar en automatisk uppdatering av deltagartabellen.
    *   **`status_pending.html`:** Kontrollerar om statusen nu är "Ready". Om så är fallet, omdirigeras sidan automatiskt till den färdiga statussidan (`status_ready.html`).

**Anslutningsindikatorer:** SSE-klienten visar anslutningsstatus i dashboarden:
- 🟢 Live — ansluten och tar emot events
- 🟡 Connecting — försöker återansluta
- 🔴 Disconnected — fallback till manuell refresh

Detta system minskar antalet API-anrop drastiskt, sänker latensen för uppdateringar från minuter till millisekunder, och ger en mycket mer responsiv upplevelse för både TOs och deltagare.
