# 6. API Referens

Detta dokument beskriver de API-endpoints som systemet exponerar och använder internt, samt de n8n-webhooks som utgör kärnan i incheckningslogiken.

---

## 1. Backend API Endpoints (via FastAPI i `backend/main.py`)

Dessa endpoints är tillgängliga via `backend`-tjänsten och är primärt avsedda för konsumtion av frontend-applikationen eller för interna Dash-komponenter.

### 1.1 `GET /api/participant/{name}/status`

*   **Beskrivning:** Hämtar en deltagares aktuella incheckningsstatus. Används främst av `status_pending.html` för att regelbundet fråga efter uppdateringar.
*   **Metod:** `GET`
*   **URL-parametrar:**
    *   `name` (path parameter, str): Namnet på deltagaren. Observera att detta fält tvättas och jämförs flexibelt med `name` och `tag` i Airtable.
*   **Svar (JSON):**
    ```json
    {
      "ready": true,                   // true om alla krav är uppfyllda
      "status": "Ready",               // "Ready" eller "Pending"
      "missing": [],                   // Lista över saknade krav (t.ex. ["Membership", "Payment"])
      "member": true,                  // true om medlemsskap är bekräftat
      "payment": true,                 // true om betalning är bekräftad
      "startgg": true,                 // true om Start.gg-registrering är bekräftad
      "name": "Deltagarens Fulla Namn", // Matchat namn
      "tag": "PlayerTag123",           // Matchad gamer-tag
      "startgg_events": ["Street Fighter 6", "Tekken 8"] // Spel registrerade för på Start.gg
    }
    ```
*   **Exempel på fel:** Ingen specifik felhantering utöver standard HTTP-felkoder (t.ex. 500 vid serverfel).

### 1.2 `GET /players`

*   **Beskrivning:** Returnerar en lista över alla deltagare. Används för närvarande inte av någon frontend-komponent men är tillgänglig.
*   **Metod:** `GET`
*   **Svar (JSON):** En array av objekt, där varje objekt representerar en deltagare.
    ```json
    [
      {
        "id": "recXXXXXXXXXXXXX",
        "name": "Deltagarens Namn",
        "email": "email@example.com",
        "tag": "PlayerTag",
        "telephone": "0701234567",
        "created": "2023-10-26T10:00:00.000Z"
      }
      // ... fler deltagare
    ]
    ```

### 1.3 `GET /event-history`

*   **Beskrivning:** Returnerar historisk eventdata. Används för närvarande inte av någon frontend-komponent men är tillgänglig.
*   **Metod:** `GET`
*   **Svar (JSON):** En array av objekt, där varje objekt representerar en historisk post.
    ```json
    [
      {
        "id": "recYYYYYYYYYYYYY",
        "event_slug": "tournament-slug",
        "status": "completed",
        "participants": 25,
        "created": "2023-09-15T12:00:00.000Z"
      }
      // ... fler historiska event
    ]
    ```

---

## 2. N8N Webhooks (proxied via `backend/main.py`)

Dessa endpoints är **n8n webhooks** som proxys genom `backend`-tjänsten. Detta innebär att anropen till dem först går via FastAPI-appen som validerar och "tvättar" data innan den skickas vidare till `n8n`.

### 2.1 `POST /n8n/webhook/checkin/validate`

*   **Beskrivning:** Den huvudsakliga endpointen för deltagare att skicka in sin incheckningsdata. Den triggar `n8n`-workflowet som utför alla verifieringar (medlemskap, Start.gg, betalning).
*   **Metod:** `POST`
*   **Content-Type:** `application/json`
*   **Request Body (JSON):**
    ```json
    {
      "namn": "Deltagarens Fulla Namn",     // Obligatorisk
      "telefon": "0701234567",             // Valfri, men rekommenderad
      "tag": "PlayerTag123",               // Obligatorisk (för Start.gg-matchning)
      "personnummer": "YYYYMMDDXXXX"       // Obligatorisk (för Sverok-matchning)
    }
    ```
*   **Query-parametrar:**
    *   `token` (valfri, str): Om `N8N_WEBHOOK_TOKEN` är konfigurerad, måste denna token inkluderas antingen som en query-parameter (`?token=your_token`) eller i headern `X-N8N-Token`.
*   **Svar (JSON):**
    *   **Framgångsrikt svar (allt klart):**
        ```json
        {
          "ready": true,
          "status": "Ready",
          "missing": [],
          "member": true,
          "payment": true,
          "startgg": true
        }
        ```
    *   **Väntande svar (något saknas):**
        ```json
        {
          "ready": false,
          "status": "Pending",
          "missing": ["Membership", "Payment"], // Lista över de krav som inte uppfyllts
          "member": false,
          "payment": false,
          "startgg": true
        }
        ```
    *   **Felsvar (från backend-proxyn, HTTP 400 Bad Request):**
        ```json
        {
          "detail": {
            "errors": ["Name is required", "Invalid personal ID format"] // Valideringsfel från backend
          }
        }
        ```
    *   **Felsvar (från backend-proxyn, HTTP 401 Unauthorized):**
        ```json
        {
          "detail": "Invalid webhook token" // Om N8N_WEBHOOK_TOKEN är felaktig eller saknas
        }
        ```
    *   **Felsvar (från backend-proxyn, HTTP 400 Bad Request):**
        ```json
        {
          "detail": "Invalid JSON payload" // Om request body inte är giltig JSON
        }
        ```

---

## 3. Autentisering och Säkerhet

*   **N8N Webhook Token:** Om miljövariabeln `N8N_WEBHOOK_TOKEN` är satt i `.env`, kommer `backend`-proxyn att kräva att denna token skickas med i varje webhook-anrop. Token kan skickas som en query-parameter (`?token=YOUR_TOKEN`) eller som en HTTP-header (`X-N8N-Token: YOUR_TOKEN`).
*   **N8N Basic Auth:** Om `N8N_BASIC_AUTH_USER` och `N8N_BASIC_AUTH_PASSWORD` är konfigurerade, kommer `backend`-proxyn att injicera dessa Basic Auth-uppgifter i anropen till `n8n` internt, om ingen annan `Authorization`-header redan finns. Detta är främst för att skydda n8n:s eget UI/API.
