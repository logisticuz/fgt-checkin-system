# 6. API Reference

This document describes the API endpoints that the system exposes. The system is divided into a `backend` service that handles check-in and status, and an `fgt_dashboard` service for administration.

---

## 1. Backend API (`backend/main.py`)

These endpoints are available through the `backend` service.

### 1.1 Check-in & Status

#### `POST /n8n/webhook/checkin/validate`
*   **Description:** The main endpoint for participants to submit their check-in data. This is a proxy that validates and sanitizes the data before forwarding it to the `n8n` workflow for verification against eBas, Start.gg, etc.
*   **Method:** `POST`
*   **Request Body (JSON):**
    ```json
    {
      "name": "Participant's Full Name",
      "phone": "0701234567",
      "tag": "PlayerTag123",
      "personal_id": "YYYYMMDDXXXX"
    }
    ```
*   **Validation (on server):**
    *   The payload is validated by `backend/validation.py`.
    *   Fields are sanitized (e.g., `personal_id` is normalized to digits only).
    *   If validation fails, `HTTP 400` is returned with a list of errors.
*   **Response (JSON):** The response comes from the `n8n` workflow and indicates the result.
    *   **If the participant is already checked in:**
        ```json
        {
          "already_checked_in": true,
          "status": "Ready",
          // ...other status fields
        }
        ```
    *   **For a new check-in:**
        ```json
        {
          "ready": false,
          "status": "Pending",
          "missing": ["Payment"],
          // ...other status fields
        }
        ```

#### `GET /api/participant/{name}/status`
*   **Description:** Retrieves a participant's current check-in status directly from Airtable. Used by `status_pending.html` to poll for updates (e.g., after a TO has manually approved a payment).
*   **Method:** `GET`
*   **URL Parameters:**
    *   `name` (str): The participant's name or tag.
*   **Response (JSON):**
    ```json
    {
      "ready": true,
      "status": "Ready",
      "missing": [],
      "member": true,
      "payment": true,
      "startgg": true,
      "name": "Participant's Name",
      "tag": "PlayerTag123",
      "startgg_events": ["Street Fighter 6"]
    }
    ```

#### `PATCH /api/player/games`
*   **Description:** Used when a player manually selects which games they want to participate in (e.g., if they were not found on Start.gg).
*   **Method:** `PATCH`
*   **Request Body (JSON):**
    ```json
    {
      "tag": "PlayerTag123",
      "slug": "tournament-slug",
      "games": ["Street Fighter 6", "Tekken 8"]
    }
    ```
*   **Response (JSON):**
    ```json
    {
      "success": true,
      "tag": "PlayerTag123",
      "games": ["Street Fighter 6", "Tekken 8"]
    }
    ```

### 1.2 Dashboard & Administration

#### `PATCH /players/{record_id}/payment`
*   **Description:** Used by the TO dashboard to manually mark a player's payment as approved or not approved.
*   **Method:** `PATCH`
*   **URL Parameters:**
    *   `record_id` (str): The Airtable record ID for the player.
*   **Request Body (JSON):**
    ```json
    { "payment_valid": true }
    ```
*   **Response (JSON):**
    ```json
    {
      "success": true,
      "record_id": "recXXXXXXXXXXXXXX",
      "payment_valid": true
    }
    ```

#### `GET /players`
*   **Description:** Retrieves a list of all players. **Note:** The call is handled by `shared/airtable_api.py`.
*   **Method:** `GET`

#### `GET /event-history`
*   **Description:** Retrieves historical event data. **Note:** The call is handled by `shared/airtable_api.py`.
*   **Method:** `GET`

### 1.3 Server-Sent Events (SSE) for Real-time Updates

These endpoints form the backbone of the real-time functionality for the dashboard.

#### `GET /api/events/stream`
*   **Description:** A client (the dashboard) connects to this endpoint to subscribe to events. The connection is kept open.
*   **Method:** `GET`
*   **Response:** A `text/event-stream` that sends events. Example:
    ```
    event: checkin
    data: {"type": "new_checkin", "name": "New Player", ...}

    : keepalive
    ```

#### `POST /api/notify/checkin` and `POST /api/notify/update`
*   **Description:** Webhooks that `n8n` calls after an operation is complete (e.g., a new check-in has been saved to Airtable). The call causes the `backend` service to send an SSE event to all connected clients.
*   **Method:** `POST`
*   **Request Body (JSON):** Flexible, contains the data to be broadcast.

### 1.4 System & Health

#### `GET /health`
*   **Description:** A lightweight health check that verifies that the internal services (`backend` and `n8n`) are responding. Used for automated monitoring.
*   **Method:** `GET`

#### `GET /health/deep`
*   **Description:** A deeper health check that also verifies the connection to external dependencies like Airtable. Should only be used for manual debugging.
*   **Method:** `GET`

---

## 2. Authentication and Security

*   **N8N Webhook Token:** If `N8N_WEBHOOK_TOKEN` is set in `.env`, calls to `/n8n/webhook/*` must include this token, either via the `token` query parameter or the `X-N8N-Token` header. This protects the `n8n` workflows from unauthorized calls.
*   **Server-side Validation:** All incoming data to `POST /n8n/webhook/checkin/validate` is validated and sanitized on the server before being processed, as a protection against incorrect or malicious data.
