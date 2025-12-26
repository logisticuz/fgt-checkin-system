# 6. API Referens

Detta dokument beskriver de API-endpoints som systemet exponerar. Systemet är uppdelat i en `backend`-tjänst som hanterar incheckning och status, och en `fgt_dashboard`-tjänst för administration.

---

## 1. Backend API (`backend/main.py`)

Dessa endpoints är tillgängliga via `backend`-tjänsten.

### 1.1 Incheckning & Status

#### `POST /n8n/webhook/checkin/validate`
*   **Beskrivning:** Huvudsaklig endpoint för deltagare att skicka in sin incheckningsdata. Detta är en proxy som validerar och sanerar datan innan den skickas vidare till `n8n`-workflowet för verifiering mot eBas, Start.gg etc.
*   **Metod:** `POST`
*   **Request Body (JSON):**
    ```json
    {
      "namn": "Deltagarens Fulla Namn",
      "telefon": "0701234567",
      "tag": "PlayerTag123",
      "personnummer": "YYYYMMDDXXXX"
    }
    ```
*   **Validering (på servern):**
    *   Payloaden valideras av `backend/validation.py`.
    *   Fält saneras (t.ex. `personnummer` normaliseras till bara siffror).
    *   Om valideringen misslyckas returneras `HTTP 400` med en lista av fel.
*   **Svar (JSON):** Svaret kommer från `n8n`-workflowet och indikerar resultatet.
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

#### `GET /api/participant/{name}/status`
*   **Beskrivning:** Hämtar en deltagares aktuella incheckningsstatus direkt från Airtable. Används av `status_pending.html` för att polla efter uppdateringar (t.ex. efter att en TO manuellt godkänt en betalning).
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
      "startgg_events": ["Street Fighter 6"]
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

### 1.2 Dashboard & Administration

#### `PATCH /players/{record_id}/payment`
*   **Beskrivning:** Används av TO-dashboarden för att manuellt markera en spelares betalning som godkänd eller icke-godkänd.
*   **Metod:** `PATCH`
*   **URL-parametrar:**
    *   `record_id` (str): Airtable record ID för spelaren.
*   **Request Body (JSON):**
    ```json
    { "payment_valid": true }
    ```
*   **Svar (JSON):**
    ```json
    {
      "success": true,
      "record_id": "recXXXXXXXXXXXXXX",
      "payment_valid": true
    }
    ```

#### `GET /players`
*   **Beskrivning:** Hämtar en lista på alla spelare. **Notering:** Anropet går via `shared/airtable_api.py`.
*   **Metod:** `GET`

#### `GET /event-history`
*   **Beskrivning:** Hämtar historiska eventdata. **Notering:** Anropet går via `shared/airtable_api.py`.
*   **Metod:** `GET`

### 1.3 Server-Sent Events (SSE) för Realtidsuppdateringar

Dessa endpoints utgör ryggraden i realtidsfunktionaliteten för dashboarden.

#### `GET /api/events/stream`
*   **Beskrivning:** En klient (dashboarden) ansluter till denna endpoint för att prenumerera på händelser. Anslutningen hålls öppen.
*   **Metod:** `GET`
*   **Svar:** En `text/event-stream` ström som skickar händelser. Exempel:
    ```
    event: checkin
    data: {"type": "new_checkin", "name": "Ny Spelare", ...}

    : keepalive
    ```

#### `POST /api/notify/checkin` och `POST /api/notify/update`
*   **Beskrivning:** Webhooks som `n8n` anropar efter att en operation är slutförd (t.ex. en ny incheckning har sparats i Airtable). Anropet får `backend`-tjänsten att skicka ut en SSE-händelse till alla anslutna klienter.
*   **Metod:** `POST`
*   **Request Body (JSON):** Flexibel, innehåller data som ska sändas.

### 1.4 System & Hälsa

#### `GET /health`
*   **Beskrivning:** En lättviktig hälsocheck som verifierar att de interna tjänsterna (`backend` och `n8n`) svarar. Används för automatiserad övervakning.
*   **Metod:** `GET`

#### `GET /health/deep`
*   **Beskrivning:** En djupare hälsocheck som även verifierar anslutningen till externa beroenden som Airtable. Ska endast användas för manuell felsökning.
*   **Metod:** `GET`

---

## 2. Autentisering och Säkerhet

*   **N8N Webhook Token:** Om `N8N_WEBHOOK_TOKEN` är satt i `.env`, måste anrop till `/n8n/webhook/*` inkludera denna token, antingen via query-parametern `token` eller `X-N8N-Token` headern. Detta skyddar `n8n`-flödena från oauktoriserade anrop.
*   **Server-side Validering:** All inkommande data till `POST /n8n/webhook/checkin/validate` valideras och saneras på servern innan den processas, som ett skydd mot felaktig eller skadlig data.
