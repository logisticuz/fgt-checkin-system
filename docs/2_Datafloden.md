# 2. Dataflöden

Det finns två huvudsakliga dataflöden i systemet: ett för administratören (TO) som konfigurerar ett event, och ett för deltagaren som checkar in.

---

### Flöde 1: Admin - Konfiguration av Event

Detta flöde beskriver hur en turneringsorganisatör (TO) förbereder systemet för ett nytt event. All interaktion sker via **FGT Dashboard**.

1.  **Öppna Instrumentpanelen:** TO navigerar till `https://admin.fgctrollhattan.se` (eller `http://localhost/admin/` i utvecklingsmiljö).
2.  **Klistra in Länk:** I "Settings"-fliken klistrar TO in den fullständiga URL:en till den specifika turneringen på Start.gg.
3.  **Hämta Eventdata:** TO klickar på knappen "Fetch Event Data".
4.  **Anrop till Start.gg:**
    *   `fgt_dashboard`-tjänsten tar emot anropet.
    *   Den extraherar turnerings-sluggen från URL:en.
    *   Den skickar en GraphQL-förfrågan till **Start.gg:s API** för att hämta turneringsinformation, inklusive namn, datum och en lista över alla spelevent som ingår.
5.  **Uppdatera Airtable:**
    *   `fgt_dashboard` ansluter sedan till **Airtable API**.
    *   Den hittar den "aktiva" raden i `settings`-tabellen (där `is_active = TRUE`).
    *   Den uppdaterar raden med den hämtade informationen: turnerings-slug, namn, datum, och en JSON-lista över alla spelevent.
6.  **Bekräftelse:** Instrumentpanelen visar ett meddelande som bekräftar att inställningarna har uppdaterats. Systemet är nu konfigurerat och redo att ta emot incheckningar för det specifika eventet.

---

### Flöde 2: Deltagare - Incheckning

Detta flöde beskriver vad som händer från det att en deltagare öppnar incheckningssidan till att statusen visas.

1.  **Öppna Incheckningssidan:** Deltagaren navigerar till `https://checkin.fgctrollhattan.se` (eller `http://localhost/` i utvecklingsmiljö). `Backend`-tjänsten serverar `checkin.html`.
2.  **Fylla i Formulär:** Deltagaren fyller i sitt namn, telefonnummer, personnummer och **tag**.
3.  **Validering (Frontend):** När deltagaren klickar på "Check In", körs `validation.js` i webbläsaren:
    *   Grundläggande kontroller utförs (t.ex. att fält inte är tomma, att personnummer har rätt längd).
    *   Om fel upptäcks, visas ett felmeddelande direkt på sidan och formuläret skickas inte.
4.  **Anrop till Backend-proxy:**
    *   Om frontend-valideringen passerar, skickas datan som en `POST`-förfrågan i `JSON`-format till `Backend`-tjänstens proxy-endpoint: `/n8n/webhook/checkin/validate`.
5.  **Validering (Backend):** `Backend`-tjänsten tar emot anropet innan det skickas till `n8n`.
    *   Den använder `validation.py` för att utföra samma validering igen på serversidan (som en säkerhetsåtgärd).
    *   Den "tvättar" även datan, t.ex. genom att ta bort bindestreck och mellanslag från personnummer och telefonnummer för att skapa ett konsekvent format.
6.  **Anrop till N8N:** Den validerade och tvättade datan skickas vidare till `n8n`-tjänstens webhook.

7.  **N8N-Workflow Exekverar:** Kärnlogiken för validering och datainsamling exekveras av en serie sammankopplade arbetsflöden i n8n. En mer detaljerad beskrivning av dessa flöden följer nedan.

8.  **Uppdatera Airtable:** Baserat på resultaten från kontrollerna ovan, skickar `n8n` en `PATCH`-förfrågan till **Airtable API** för att uppdatera (eller skapa) deltagarens rad i `active_event_data`-tabellen med deras status (`Ready`, `Pending`, etc.).

9.  **Svar till Frontend:** `n8n` skickar ett `JSON`-svar tillbaka till `Backend`-proxyn, som i sin tur skickar det till användarens webbläsare. Svaret innehåller information om statusen. Exempel: `{"ready": false, "missing": ["Membership"]}`.

10. **Omdirigering (Redirect):** JavaScript-koden i `checkin.html` tar emot svaret och agerar baserat på innehållet:
    *   **Om `ready` är `true`:** Användaren omdirigeras till sin personliga statussida (`status_ready.html`), där de ser en bekräftelse på att allt är klart.
    *   **Om `ready` är `false`:** Användaren omdirigeras till registreringssidan (`register.html`) med query-parametrar som indikerar vad som saknas. Denna sida visar dynamiskt de formulär som krävs, inklusive en Swish-integration med QR-kod (desktop) eller deep link (mobil) om betalning saknas.

### Logik för 'Ready'-status

För att en deltagare ska få statusen `Ready` måste **samtliga** av följande villkor vara uppfyllda:
*   `member` är `true`.
*   `startgg` är `true`.
*   `payment_valid` är `true`.

Denna logik är för närvarande **hårdkodad** i systemet (`backend/main.py` och `fgt_dashboard/callbacks.py`). Funktionalitet för att göra dessa krav konfigurerbara via `settings`-tabellen i Airtable är planerad men inte implementerad.

---

### Djupdykning: N8N-arbetsflöden

Nedan beskrivs de aktiva n8n-arbetsflödena som hanterar incheckningsprocessen.

#### `Checkin_Orchestrator.json` (Huvud-orkestratorn)

Detta är det centrala arbetsflödet som orkestrerar hela incheckningsprocessen.

*   **Trigger:** `Webhook` (POST `/webhook/checkin/validate`) - Tar emot incheckningsdata från `backend`-tjänsten.
*   **Load Settings:** Hämtar aktiva inställningar från Airtable (`settings`-tabellen).
*   **Parse Input:** Extraherar och transformerar data från webhook och inställningar.
*   **Check Duplicate:** Kontrollerar om spelaren redan är incheckad.
*   **IF Duplicate:** Returnerar den befintliga statusen om spelaren hittas.
*   **eBas Check & Start.gg Check:** Anropar sub-workflows för att verifiera medlemskap och turneringsregistrering.
*   **Merge Results:** Sammanställer resultaten, genererar en `UUID`, sätter initial `status` ("Pending") och `is_guest`-flaggan baserat på Start.gg-resultatet.
*   **Save to Airtable:** Skapar en ny post i `active_event_data`-tabellen.
*   **Post-Save Duplicate Handling:** Inkluderar noder för att hantera "race conditions".
*   **Notify Dashboard:** Anropar `http://backend:8000/api/notify/checkin` för att trigga en SSE broadcast.
*   **Return Results:** Returnerar det slutliga resultatet.

#### `eBas_Membership_Check.json` (Medlemskapskontroll)

Detta arbetsflöde validerar en deltagares medlemskap i Sverok.

*   **Trigger:** `Webhook` (POST `/webhook/ebas/check`) - Tar emot `personnummer`.
*   **Normalize Personnummer:** Validerar och normaliserar personnumret.
*   **Call eBas confirm_membership:** Anropar Sveroks eBas API.
*   **Parse Response:** Parsar svaret och returnerar `isMember` (true/false).

#### `eBas_Register.json` (eBas-registrering)

Detta arbetsflöde registrerar nya medlemmar i Sverok.

*   **Trigger:** `Webhook` (POST `/webhook/ebas/register`) - Tar emot medlemsdata.
*   **Normalize Input:** Validerar och normaliserar indata.
*   **Call eBas API:** Anropar Sveroks eBas API för registrering.
*   **Parse Response:** Parsar svaret för att bekräfta framgång.

#### `Startgg_Check.json` (Start.gg-verifiering)

Detta arbetsflöde verifierar om en spelare är registrerad i en specifik Start.gg-turnering.

*   **Trigger:** `Webhook` (POST `/webhook/startgg/check`) - Tar emot `tag` och `slug`.
*   **Parse Input:** Validerar och rensar indata.
*   **Query Start.gg:** Anropar Start.gg:s GraphQL API.
*   **Parse Response:** Parsar svaret och returnerar `isRegistered` (true/false).

---

### Flöde 3: Realtidsuppdatering (Server-Sent Events)

Systemet använder Server-Sent Events (SSE) för att skicka omedelbara uppdateringar till klienter (både FGT Dashboard och deltagarens statussida) utan behov av kontinuerlig polling.

1.  **Klient Ansluter:** När **FGT Dashboard** eller en deltagares `status_pending.html`-sida laddas, ansluter en JavaScript-klient (`sse-client.js`) till `GET /api/events/stream` på `Backend`-tjänsten. Detta håller en öppen anslutning.
2.  **Händelse Sker:** En händelse som kräver en uppdatering inträffar. Det finns två huvudtyper:
    *   **Ny incheckning:** `n8n`-flödet slutförs och gör ett `POST`-anrop till `/api/notify/checkin`.
    *   **Manuell uppdatering:** En TO klickar på en knapp i dashboarden (t.ex. godkänner en betalning). Dashboarden anropar en API-endpoint (t.ex. `PATCH /players/{id}/payment`), som i sin tur anropar `/api/notify/update`.
3.  **Backend Broadcast:** `Backend`-tjänstens `SSEManager` tar emot notifikationen från anropet i steg 2.
4.  **Event Skickas:** `SSEManager` skickar omedelbart ett data-paket över alla öppna anslutningar som skapades i steg 1.
5.  **Klient Agerar:** JavaScript-klienten på respektive sida tar emot eventet:
    *   **Dashboard:** Triggar en automatisk uppdatering av deltagartabellen.
    *   **`status_pending.html`:** Kontrollerar om statusen nu är "Ready". Om så är fallet, omdirigeras sidan automatiskt till den färdiga statussidan (`status_ready.html`).

Detta system minskar antalet API-anrop drastiskt, sänker latensen för uppdateringar från minuter till millisekunder, och ger en mycket mer responsiv upplevelse för både TOs och deltagare.
