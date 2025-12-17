# 6. API Reference

This document describes the API endpoints that the system exposes and uses internally, as well as the n8n webhooks that form the core of the check-in logic.

---

## 1. Backend API Endpoints (via FastAPI in `backend/main.py`)

These endpoints are available through the `backend` service and are primarily intended for consumption by the frontend application or for internal Dash components.

### 1.1 `GET /api/participant/{name}/status`

*   **Description:** Retrieves a participant's current check-in status. Used primarily by `status_pending.html` to periodically poll for updates.
*   **Method:** `GET`
*   **URL Parameters:**
    *   `name` (path parameter, str): The name of the participant. Note that this field is sanitized and compared flexibly with `name` and `tag` in Airtable.
*   **Response (JSON):**
    ```json
    {
      "ready": true,                   // true if all requirements are met
      "status": "Ready",               // "Ready" or "Pending"
      "missing": [],                   // List of missing requirements (e.g., ["Membership", "Payment"])
      "member": true,                  // true if membership is confirmed
      "payment": true,                 // true if payment is confirmed
      "startgg": true,                 // true if Start.gg registration is confirmed
      "name": "Participant's Full Name", // Matched name
      "tag": "PlayerTag123",           // Matched tag
      "startgg_events": ["Street Fighter 6", "Tekken 8"] // Games registered for on Start.gg
    }
    ```
*   **Error Example:** No specific error handling other than standard HTTP error codes (e.g., 500 on server error).

### 1.2 `GET /players`

*   **Description:** Returns a list of all players. Currently not used by any frontend component but is available.
*   **Method:** `GET`
*   **Response (JSON):** An array of objects, where each object represents a player.
    ```json
    [
      {
        "id": "recXXXXXXXXXXXXX",
        "name": "Participant's Name",
        "email": "email@example.com",
        "tag": "PlayerTag",
        "telephone": "0701234567",
        "created": "2023-10-26T10:00:00.000Z"
      }
      // ... more participants
    ]
    ```

### 1.3 `GET /event-history`

*   **Description:** Returns historical event data. Currently not used by any frontend component but is available.
*   **Method:** `GET`
*   **Response (JSON):** An array of objects, where each object represents a historical record.
    ```json
    [
      {
        "id": "recYYYYYYYYYYYYY",
        "event_slug": "tournament-slug",
        "status": "completed",
        "participants": 25,
        "created": "2023-09-15T12:00:00.000Z"
      }
      // ... more historical events
    ]
    ```

---

## 2. N8N Webhooks (proxied via `backend/main.py`)

These endpoints are **n8n webhooks** that are proxied through the `backend` service. This means that calls to them first go through the FastAPI app, which validates and "sanitizes" data before forwarding it to `n8n`.

### 2.1 `POST /n8n/webhook/checkin/validate`

*   **Description:** The main endpoint for participants to submit their check-in data. It triggers the `n8n` workflow that performs all verifications (membership, Start.gg, payment).
*   **Method:** `POST`
*   **Content-Type:** `application/json`
*   **Request Body (JSON):**
    ```json
    {
      "namn": "Participant's Full Name", // Required
      "telefon": "0701234567",             // Optional, but recommended
      "tag": "PlayerTag123",               // Required (for Start.gg matching)
      "personnummer": "YYYYMMDDXXXX"       // Required (for Sverok matching)
    }
    ```
*   **Query Parameters:**
    *   `token` (optional, str): If `N8N_WEBHOOK_TOKEN` is configured, this token must be included either as a query parameter (`?token=your_token`) or in the `X-N8N-Token` header.
*   **Response (JSON):**
    *   **Success Response (all clear):**
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
    *   **Pending Response (something is missing):**
        ```json
        {
          "ready": false,
          "status": "Pending",
          "missing": ["Membership", "Payment"], // List of unmet requirements
          "member": false,
          "payment": false,
          "startgg": true
        }
        ```
    *   **Error Response (from backend proxy, HTTP 400 Bad Request):**
        ```json
        {
          "detail": {
            "errors": ["Name is required", "Invalid personal ID format"] // Validation errors from backend
          }
        }
        ```
    *   **Error Response (from backend proxy, HTTP 401 Unauthorized):**
        ```json
        {
          "detail": "Invalid webhook token" // If N8N_WEBHOOK_TOKEN is incorrect or missing
        }
        ```
    *   **Error Response (from backend proxy, HTTP 400 Bad Request):**
        ```json
        {
          "detail": "Invalid JSON payload" // If the request body is not valid JSON
        }
        ```

---

## 3. Authentication and Security

*   **N8N Webhook Token:** If the `N8N_WEBHOOK_TOKEN` environment variable is set in `.env`, the `backend` proxy will require this token to be sent with every webhook call. The token can be sent as a query parameter (`?token=YOUR_TOKEN`) or as an HTTP header (`X-N8N-Token: YOUR_TOKEN`).
*   **N8N Basic Auth:** If `N8N_BASIC_AUTH_USER` and `N8N_BASIC_AUTH_PASSWORD` are configured, the `backend` proxy will inject these Basic Auth credentials into the calls to `n8n` internally, if no other `Authorization` header is already present. This is primarily to protect n8n's own UI/API.
