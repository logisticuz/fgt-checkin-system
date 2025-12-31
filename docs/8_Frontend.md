# 8. Frontend

Detta dokument beskriver de viktigaste delarna av frontend-logiken och designen i systemet. Frontend består av statiska HTML-filer som serveras av `backend`-tjänsten, med dynamisk funktionalitet som drivs av JavaScript.

---

## 1. Designsystem och Enhetligt UI

Alla fyra huvudsakliga frontend-sidor (`checkin.html`, `register.html`, `status_pending.html`, `status_ready.html`) delar ett enhetligt designsystem för en konsekvent användarupplevelse.

*   **Layout:** En centrerad container med en maxbredd på `480px`.
*   **Bakgrund:** En mörk gradient (`linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 100%)`).
*   **Färger:**
    *   Primärfärg (länkar, knappar): `#58aaff` (ljusblå).
    *   Successfärg (bekräftelser): `#4caf50` (grön).
*   **Kort:** Innehållsblock har en lätt genomskinlig bakgrund (`rgba(255, 255, 255, 0.05)`) och rundade hörn (`16px`).
*   **Logotyp:** Logotypen visas nu konsekvent i headern på alla sidor.

---

## 2. Incheckningsformuläret (`checkin.html`)

### Validering (`static/js/validation.js`)

Innan formuläret skickas körs en validering på klientsidan för att ge omedelbar feedback till användaren.

*   **Funktion:** `validateForm()` anropas när användaren klickar på "Check In".
*   **Valideringsregler:**
    *   Alla fält (`namn`, `tag`, `telefon`) måste vara ifyllda.
    *   Personnummer måste vara ett giltigt format (10 eller 12 siffror, Luhn-algoritmen validerar).
    *   Telefonnummer måste ha minst 7 siffror.
    *   Maxlängder för fält för att förhindra missbruk.
*   **Felhantering:** Om valideringen misslyckas, visas felmeddelanden dynamiskt på sidan och formuläret skickas inte.
*   **Säkerhet:** En identisk validering och sanering sker alltid på serversidan (`backend/validation.py`) som ett andra skyddslager.
*   **Personnummer:** Efter en lyckad incheckning rensas personnumret från `localStorage` för att inte lämna känslig data i webbläsaren.

---

## 3. Registreringssidan (`register.html`)

Denna sida visas när en deltagare saknar något av de nödvändiga kraven för att bli "Ready".

### Manuell Spelval
Om en spelare inte hittades på Start.gg, kan de manuellt välja vilka spel de ska delta i.
*   **UI:** En serie checkboxar (en för varje spel) visas, vilket är mer användarvänligt än en traditionell multi-select.
*   **Logik:** När spelaren bekräftar sina val, skickas en `PATCH`-förfrågan till `/api/player/games` för att uppdatera deras `tournament_games_registered`-fält i Airtable.

### Swish-integration
För att förenkla betalningsprocessen har en adaptiv Swish-integration implementerats.
*   **Enhetsdetektering:** Ett JavaScript känner av om användaren är på en mobil enhet eller en dator.
*   **Desktop-vy:** En QR-kod visas som kan skannas direkt med Swish-appen. Swish-numret visas också som en fallback.
*   **Mobil-vy:** En "Öppna Swish"-knapp visas. Klickar man på den öppnas Swish-appen automatiskt via en **deep link** (`swish://payment?data=...`).
*   **Förifylld Data:** Deep-länken är förifylld med:
    *   Korrekt Swish-nummer.
    *   Korrekt belopp.
    *   Spelarens **tag** som meddelande, vilket underlättar för TO:s att matcha betalningen.

---

## 4. Statussidan (`status_pending.html`)

Denna sida visas medan en spelare väntar på att alla krav ska uppfyllas (oftast manuellt godkännande av betalning).

### Status-tabell
Istället för otydliga badges visas en tydlig, färgkodad tabell med två kolumner: "READY" och "MISSING".
*   **READY-kolumnen (grön bakgrund):** Listar alla krav som är uppfyllda (t.ex. `✓ Member`).
*   **MISSING-kolumnen (röd bakgrund):** Listar alla krav som saknas (t.ex. `✗ Payment`).

### Realtidsuppdateringar via SSE
Sidan förlitar sig inte längre på ineffektiv polling.
*   **SSE-klient:** `sse-client.js` ansluter till `Backend`-tjänstens SSE-ström.
*   **Event-lyssnare:** När ett `update`-event tas emot (t.ex. när en TO godkänner en betalning), kontrollerar JavaScript-koden spelarens nya status via ett `GET`-anrop till `/api/participant/{name}/status`.
*   **Automatisk omdirigering:** Om den nya statusen är "Ready", omdirigeras sidan automatiskt till `status_ready.html` utan att användaren behöver göra något.
*   **Manuell Refresh:** En refresh-knapp finns kvar som en fallback.

---

## 5. SSE-klienten (`assets/sse-client.js`)

Detta är en delad JavaScript-modul som används av **FGT Dashboard** för att hantera anslutningen till SSE-strömmen.

*   **Anslutning:** Initierar en `EventSource`-anslutning till `/api/events/stream`.
*   **Anslutningsindikator:** Uppdaterar en visuell indikator i UI:t för att visa anslutningsstatus (Live, Connecting, Disconnected).
*   **Event-hantering:** När ett event (t.ex. `checkin` eller `update`) tas emot, anropas en callback-funktion som triggar en uppdatering av datan i dashboarden.
*   **Automatisk Reconnect:** Om anslutningen bryts försöker klienten automatiskt att återansluta.
*   **Fallback till Polling:** Om SSE-anslutningen misslyckas upprepade gånger, faller klienten tillbaka till att polla API:et med jämna mellanrum (t.ex. var 30:e sekund) som en säkerhetsåtgärd.
