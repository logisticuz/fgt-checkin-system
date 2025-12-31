# 2. Data Flows

There are two main data flows in the system: one for the administrator (TO) who configures an event, and one for the participant who checks in.

---

### Flow 1: Admin - Event Configuration

This flow describes how a Tournament Organizer (TO) prepares the system for a new event. All interaction happens via the **FGT Dashboard**.

1.  **Open the Dashboard:** The TO navigates to `https://admin.fgctrollhattan.se` (or `http://localhost/admin/` in a development environment).
2.  **Paste Link:** In the "Settings" tab, the TO pastes the full URL to the specific tournament on Start.gg.
3.  **Fetch Event Data:** The TO clicks the "Fetch Event Data" button.
4.  **Call to Start.gg:**
    *   The `fgt_dashboard` service receives the request.
    *   It extracts the tournament slug from the URL.
    *   It sends a GraphQL request to the **Start.gg API** to retrieve tournament information, including name, date, and a list of all included game events.
5.  **Update Airtable:**
    *   The `fgt_dashboard` then connects to the **Airtable API**.
    *   It finds the "active" row in the `settings` table (where `is_active = TRUE`).
    *   It updates the row with the fetched information: tournament slug, name, date, and a JSON list of all game events.
6.  **Confirmation:** The dashboard displays a message confirming that the settings have been updated. The system is now configured and ready to receive check-ins for that specific event.

---

### Flow 2: Participant - Check-in

This flow describes what happens from the moment a participant opens the check-in page until their status is displayed.

1.  **Open Check-in Page:** The participant navigates to `https://checkin.fgctrollhattan.se` (or `http://localhost/` in a development environment). The `backend` service serves `checkin.html`.
2.  **Fill in Form:** The participant fills in their name, phone number, personal ID number (`personnummer`), and **tag**.
3.  **Validation (Frontend):** When the participant clicks "Check In", `validation.js` runs in the browser:
    *   Basic checks are performed (e.g., fields are not empty, personal ID has the correct length).
    *   If errors are detected, an error message is displayed directly on the page, and the form is not submitted.
4.  **Call to Backend Proxy:**
    *   If the frontend validation passes, the data is sent as a `POST` request in `JSON` format to the `backend` service's proxy endpoint: `/n8n/webhook/checkin/validate`.
5.  **Validation (Backend):** The `backend` service receives the request before it is sent to `n8n`.
    *   It uses `validation.py` to perform the same validation again on the server-side (as a security measure).
    *   It also "sanitizes" the data, e.g., by removing hyphens and spaces from personal ID and phone numbers to create a consistent format.
6.  **Call to N8N:** The validated and sanitized data is forwarded to the `n8n` service's webhook.

7.  **N8N Workflow Executes:** The core logic for validation and data collection is executed by a series of interconnected workflows in n8n. A more detailed description of these flows follows below.

8.  **Update Airtable:** Based on the results of the checks above, `n8n` sends a `PATCH` request to the **Airtable API** to update (or create) the participant's row in the `active_event_data` table with their status (`Ready`, `Pending`, etc.).

9.  **Response to Frontend:** `n8n` sends a `JSON` response back to the `backend` proxy, which in turn sends it to the user's browser. The response contains information about the status. Example: `{"ready": false, "missing": ["Membership"]}`.

10. **Redirection:** The JavaScript code in `checkin.html` receives the response and acts based on its content:
    *   **If `ready` is `true`:** The user is redirected to their personal status page (`status_ready.html`), where they see a confirmation that everything is complete.
    *   **If `ready` is `false`:** The user is redirected to the registration page (`register.html`) with query parameters indicating what is missing. This page will then dynamically display the required forms, including a Swish integration with a QR code (desktop) or deep link (mobile) if payment is required.

### 'Ready' Status Logic

For a participant to achieve the `Ready` status, **all** of the following conditions must be met:
*   `member` is `true`.
*   `startgg` is `true`.
*   `payment_valid` is `true`.

This logic is currently **hardcoded** in the system (`backend/main.py` and `fgt_dashboard/callbacks.py`). Functionality to make these requirements configurable via the `settings` table in Airtable is planned but not yet implemented.

---

### In-Depth: N8N Workflows

Below is a description of the active n8n workflows that handle the check-in process.

#### `Checkin_Orchestrator.json` (Main Orchestrator)

This is the central workflow that orchestrates the entire check-in process.

*   **Trigger:** `Webhook` (POST `/webhook/checkin/validate`) - Receives check-in data from the `backend` service.
*   **Load Settings:** Fetches active settings from Airtable (`settings` table).
*   **Parse Input:** Extracts and transforms data from the webhook and settings.
*   **Check Duplicate:** Checks if the player is already checked in.
*   **IF Duplicate:** Returns the existing status if the player is found.
*   **eBas Check & Start.gg Check:** Calls sub-workflows to verify membership and tournament registration.
*   **Merge Results:** Compiles the results, generates a `UUID`, sets the initial `status` ("Pending") and the `is_guest` flag based on the Start.gg result.
*   **Save to Airtable:** Creates a new record in the `active_event_data` table.
*   **Post-Save Duplicate Handling:** Includes nodes to handle race conditions.
*   **Notify Dashboard:** Calls `http://backend:8000/api/notify/checkin` to trigger an SSE broadcast.
*   **Return Results:** Returns the final result of the process.

#### `eBas_Membership_Check.json` (Membership Check)

This workflow validates a participant's membership in Sverok.

*   **Trigger:** `Webhook` (POST `/webhook/ebas/check`) - Receives a `personnummer`.
*   **Normalize Personnummer:** Validates and normalizes the personal ID number.
*   **Call eBas confirm_membership:** Calls the Sverok eBas API.
*   **Parse Response:** Parses the response and returns `isMember` (true/false).

#### `eBas_Register.json` (eBas Registration)

This workflow registers new members in the Sverok eBas system.

*   **Trigger:** `Webhook` (POST `/webhook/ebas/register`) - Receives member data.
*   **Normalize Input:** Validates and normalizes the input data.
*   **Call eBas API:** Calls the Sverok eBas API for registration.
*   **Parse Response:** Parses the response to confirm success.

#### `Startgg_Check.json` (Start.gg Verification)

This workflow verifies if a player is registered for a specific Start.gg tournament.

*   **Trigger:** `Webhook` (POST `/webhook/startgg/check`) - Receives a `tag` and `slug`.
*   **Parse Input:** Validates and cleans the input data.
*   **Query Start.gg:** Calls the Start.gg GraphQL API.
*   **Parse Response:** Parses the response and returns `isRegistered` (true/false).

---

### Flow 3: Real-time Updates (Server-Sent Events)

The system uses Server-Sent Events (SSE) to push instant updates to clients (both the FGT Dashboard and the participant's status page) without the need for continuous polling.

1.  **Client Connects:** When the **FGT Dashboard** or a participant's `status_pending.html` page loads, a JavaScript client (`sse-client.js`) connects to `GET /api/events/stream` on the `Backend` service. This keeps a connection open.
2.  **Event Occurs:** An event that requires an update happens. There are two main types:
    *   **New Check-in:** The `n8n` workflow finishes and makes a `POST` call to `/api/notify/checkin`.
    *   **Manual Update:** A TO clicks a button in the dashboard (e.g., approving a payment). The dashboard calls an API endpoint (e.g., `PATCH /players/{id}/payment`), which in turn calls `/api/notify/update`.
3.  **Backend Broadcast:** The `Backend` service's `SSEManager` receives the notification from the call in step 2.
4.  **Event is Sent:** The `SSEManager` immediately sends a data packet over all open connections established in step 1.
5.  **Client Acts:** The JavaScript client on the respective page receives the event:
    *   **Dashboard:** Triggers an automatic refresh of the participant table.
    *   **`status_pending.html`:** Checks if the status is now "Ready". If so, the page is automatically redirected to the final status page (`status_ready.html`).

This system drastically reduces the number of API calls, lowers latency for updates from minutes to milliseconds, and provides a much more responsive experience for both TOs and participants.

