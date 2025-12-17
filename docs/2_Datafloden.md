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
7.  **N8N-Workflow Exekverar:** Detta är kärnlogiken. `n8n` utför en serie automatiska kontroller:
    *   **Start.gg-kontroll:** Använder deltagarens **tag** för att anropa **Start.gg API** och verifiera att de är registrerade för det aktiva eventet.
    *   **Medlemskapskontroll:** Använder deltagarens personnummer för att anropa **Sverok eBas API** och verifiera att de är medlemmar i föreningen.
    *   **Betalningskontroll:** Kontrollerar betalningsstatus (detta kan vara en intern logik eller en integration med en betaltjänst).
8.  **Uppdatera Airtable:** Baserat på resultaten från kontrollerna ovan, skickar `n8n` en `PATCH`-förfrågan till **Airtable API** för att uppdatera (eller skapa) deltagarens rad i `active_event_data`-tabellen med deras status (`Ready`, `Pending`, etc.).
9.  **Svar till Frontend:** `n8n` skickar ett `JSON`-svar tillbaka till `Backend`-proxyn, som i sin tur skickar det till användarens webbläsare. Svaret innehåller information om statusen. Exempel: `{"ready": false, "missing": ["Membership"]}`.
10. **Omdirigering (Redirect):** JavaScript-koden i `checkin.html` tar emot svaret och agerar baserat på innehållet:
    *   **Om `ready` är `true`:** Användaren omdirigeras till sin personliga statussida, t.ex. `/status/Pelle-Persson`, där de ser en bekräftelse på att allt är klart.
    *   **Om `ready` är `false`:** Användaren omdirigeras till registreringssidan med query-parametrar som indikerar vad som saknas, t.ex. `/register?name=Pelle-Persson&ebas=true`. Denna sida kommer då att dynamiskt visa de formulär som krävs för att åtgärda de saknade stegen.

---

### Flöde 3: Status-polling (Väntande deltagare)

Detta flöde beskriver vad som händer när en deltagare väntar på att alla krav ska uppfyllas (t.ex. väntar på att betalning ska bekräftas).

1.  **Statussidan visas:** Om deltagaren inte är helt klar efter incheckning, renderar `Backend` sidan `status_pending.html`.
2.  **Polling startar:** JavaScript på sidan börjar automatiskt polla `Backend`-tjänstens API-endpoint var 10:e sekund.
3.  **API-anrop:** Sidan anropar `GET /api/participant/{name}/status` för att hämta aktuell status.
4.  **Backend läser från Airtable:** `Backend`-tjänsten läser deltagarens rad direkt från **Airtable** och returnerar aktuell status för `member`, `payment` och `startgg`.
5.  **Uppdatering av UI:** Sidan uppdaterar checkmarkeringar och visar vad som fortfarande saknas.
6.  **Omdirigering vid success:** När alla krav är uppfyllda (`ready: true`), omdirigeras deltagaren automatiskt till `status_ready.html`.
7.  **Timeout:** Efter 5 minuter (30 polls) upphör automatisk polling för att spara resurser.

#### Designbeslut: Varför Backend API istället för N8N?

Ursprungligen pollades `n8n`-webhooken direkt från `status_pending.html`. Detta byttes ut till ett dedikerat Backend API av följande anledningar:

| Aspekt | N8N Webhook | Backend API |
|--------|-------------|-------------|
| **Datakälla** | Live-validering mot externa API:er (eBas, Start.gg) | Läser cached status från Airtable |
| **Payment-status** | ❌ Vet inte om payment (lagras bara i Airtable) | ✅ Läser payment direkt från Airtable |
| **Konsistens** | Olika resultat beroende på vilken data som skickas | Samma källa som backend använder för routing |
| **Prestanda** | Tungt - anropar externa API:er varje gång | Lätt - bara en Airtable-läsning |
| **Rate limits** | Risk att överskrida eBas/Start.gg rate limits | Ingen risk - bara intern databasläsning |

**Kärnan i problemet:** N8N-orchestratorn är designad för *initial validering* vid incheckning. Den anropar externa API:er (eBas, Start.gg) för att verifiera medlemskap och turneringsregistrering. Men den vet *inte* om betalningsstatus, som hanteras separat via Swish och lagras manuellt i Airtable.

När `status_pending.html` pollade n8n, kunde n8n svara "ready: true" (eftersom inga externa valideringar behövdes) medan `Backend` visade pending-sidan (eftersom payment saknades i Airtable). Detta skapade en inkonsekvent loop.

**Lösningen:** Låt `status_pending.html` polla samma datakälla som `Backend` använder för att bestämma vilken sida som ska visas. Nu är systemet konsekvent: om du ser pending-sidan, kommer polling också visa pending tills Airtable uppdateras.
