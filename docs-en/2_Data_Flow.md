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
7.  **N8N Workflow Executes:** This is the core logic. `n8n` performs a series of automated checks:
    *   **Start.gg Check:** Uses the participant's **tag** to call the **Start.gg API** and verify that they are registered for the active event.
    *   **Membership Check:** Uses the participant's personal ID number to call the **Sverok eBas API** and verify that they are a member of the association.
    *   **Payment Check:** Checks the payment status (this could be internal logic or an integration with a payment service).
8.  **Update Airtable:** Based on the results of the checks above, `n8n` sends a `PATCH` request to the **Airtable API** to update (or create) the participant's row in the `active_event_data` table with their status (`Ready`, `Pending`, etc.).
9.  **Response to Frontend:** `n8n` sends a `JSON` response back to the `backend` proxy, which in turn sends it to the user's browser. The response contains information about the status. Example: `{"ready": false, "missing": ["Membership"]}`.
10. **Redirection:** The JavaScript code in `checkin.html` receives the response and acts based on its content:
    *   **If `ready` is `true`:** The user is redirected to their personal status page, e.g., `/status/John-Doe`, where they see a confirmation that everything is complete.
    *   **If `ready` is `false`:** The user is redirected to the registration page with query parameters indicating what is missing, e.g., `/register?name=John-Doe&ebas=true`. This page will then dynamically display the forms required to fix the missing steps.
