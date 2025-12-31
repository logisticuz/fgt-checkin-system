# 8. Frontend

This document describes the most important parts of the frontend logic and design in the system. The frontend consists of static HTML files served by the `backend` service, with dynamic functionality powered by JavaScript.

---

## 1. Design System and Unified UI

All four main frontend pages (`checkin.html`, `register.html`, `status_pending.html`, `status_ready.html`) share a unified design system for a consistent user experience.

*   **Layout:** A centered container with a max-width of `480px`.
*   **Background:** A dark gradient (`linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 100%)`).
*   **Colors:**
    *   Primary color (links, buttons): `#58aaff` (light blue).
    *   Success color (confirmations): `#4caf50` (green).
*   **Cards:** Content blocks have a slightly transparent background (`rgba(255, 255, 255, 0.05)`) and rounded corners (`16px`).
*   **Logo:** The logo is now consistently displayed in the header of all pages.

---

## 2. Check-in Form (`checkin.html`)

### Validation (`static/js/validation.js`)

Before the form is submitted, a client-side validation is run to provide immediate feedback to the user.

*   **Function:** `validateForm()` is called when the user clicks "Check In".
*   **Validation Rules:**
    *   All fields (`name`, `tag`, `phone`) must be filled.
    *   Personal ID number (`personnummer`) must be a valid format (10 or 12 digits, Luhn algorithm validation).
    *   Phone number must have at least 7 digits.
    *   Max length enforcement on fields to prevent abuse.
*   **Error Handling:** If validation fails, error messages are dynamically displayed on the page, and the form is not submitted.
*   **Security:** An identical validation and sanitization process always occurs on the server-side (`backend/validation.py`) as a second layer of defense.
*   **Personal ID:** After a successful check-in, the personal ID number is cleared from `localStorage` to avoid leaving sensitive data in the browser.

---

## 3. Registration Page (`register.html`)

This page is displayed when a participant is missing any of the necessary requirements to become "Ready".

### Manual Game Selection
If a player was not found on Start.gg, they can manually select which games they will participate in.
*   **UI:** A series of checkboxes (one for each game) is displayed, which is more user-friendly than a traditional multi-select.
*   **Logic:** When the player confirms their selection, a `PATCH` request is sent to `/api/player/games` to update their `tournament_games_registered` field in Airtable.

### Swish Integration
To simplify the payment process, an adaptive Swish integration has been implemented.
*   **Device Detection:** JavaScript detects whether the user is on a mobile device or a desktop computer.
*   **Desktop View:** A QR code is displayed, which can be scanned directly with the Swish app. The Swish number is also shown as a fallback.
*   **Mobile View:** An "Open Swish" button is displayed. Clicking it automatically opens the Swish app via a **deep link** (`swish://payment?data=...`).
*   **Pre-filled Data:** The deep link is pre-filled with:
    *   The correct Swish number.
    *   The correct amount.
    *   The player's **tag** as the message, which makes it easier for TOs to match the payment.

---

## 4. Status Page (`status_pending.html`)

This page is displayed while a player is waiting for all requirements to be met (usually manual approval of payment).

### Status Table
Instead of unclear badges, a clear, color-coded table with two columns is displayed: "READY" and "MISSING".
*   **READY Column (green background):** Lists all requirements that are met (e.g., `✓ Member`).
*   **MISSING Column (red background):** Lists all requirements that are missing (e.g., `✗ Payment`).

### Real-time Updates via SSE
The page no longer relies on inefficient polling.
*   **SSE Client:** `sse-client.js` connects to the `Backend` service's SSE stream.
*   **Event Listener:** When an `update` event is received (e.g., when a TO approves a payment), the JavaScript code checks the player's new status via a `GET` call to `/api/participant/{name}/status`.
*   **Automatic Redirect:** If the new status is "Ready", the page is automatically redirected to `status_ready.html` without the user having to do anything.
*   **Manual Refresh:** A refresh button remains as a fallback.

---

## 5. The SSE Client (`assets/sse-client.js`)

This is a shared JavaScript module used by the **FGT Dashboard** to manage the connection to the SSE stream.

*   **Connection:** Initializes an `EventSource` connection to `/api/events/stream`.
*   **Connection Indicator:** Updates a visual indicator in the UI to show the connection status (Live, Connecting, Disconnected).
*   **Event Handling:** When an event (e.g., `checkin` or `update`) is received, a callback function is called that triggers an update of the data in the dashboard.
*   **Automatic Reconnect:** If the connection is broken, the client automatically tries to reconnect.
*   **Fallback to Polling:** If the SSE connection fails repeatedly, the client falls back to polling the API at regular intervals (e.g., every 30 seconds) as a safety measure.
