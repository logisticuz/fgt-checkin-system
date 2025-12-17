# 4. External Dependencies

The system relies on several external services and APIs to function. A working connection and correct API keys for these services are crucial for the system's operation.

---

### 1. Airtable

*   **Role:** The system's central database.
*   **Usage:**
    *   **Configuration Storage:** The `settings` table stores information about the active event, which is fetched from Start.gg. This table is written to by the **FGT Dashboard**.
    *   **Live Data Storage:** The `active_event_data` table contains a row for each participant who checks in. It stores their status (`Ready`, `Pending`), personal information, and which requirements they meet. This table is primarily written to by **N8N** and read by both the **Backend** and the **FGT Dashboard**.
*   **Requirements:**
    *   An Airtable account.
    *   An Airtable Base with the necessary tables (`settings`, `active_event_data`).
    *   `AIRTABLE_API_KEY`: An API key with permission to read and write to the base.
    *   `AIRTABLE_BASE_ID`: The ID of the specific base to be used.
    *   Both of these values must be configured in the `.env` file.

---

### 2. Start.gg

*   **Role:** Source for all tournament and participant information.
*   **Usage:**
    *   **Event Configuration:** The **FGT Dashboard** uses Start.gg's GraphQL API to retrieve details about a tournament (name, date, included game events) when a TO pastes a tournament link.
    *   **Participant Validation:** The **N8N** workflow uses the participant's tag to call the Start.gg API and verify that they are correctly registered for the active event.
*   **Requirements:**
    *   A Start.gg account.
    *   `STARTGG_API_KEY`: A personal API key to make calls to the GraphQL API. This must be configured in the `.env` file.
    *   For the administrative OAuth login, `STARTGG_CLIENT_ID` and `STARTGG_CLIENT_SECRET` are also required.

---

### 3. Sverok eBas

*   **Role:** Source for verifying membership in the affiliated association.
*   **Usage:**
    *   **Membership Validation:** The **N8N** workflow calls the eBas API with a participant's personal ID number (`personnummer`) to check if they are an active, paying member of the association.
    *   **New Member Registration:** If a participant is not a member, the dynamic registration form can use the eBas API to register a new member directly.
*   **Requirements:**
    *   Access to the association's API keys for eBas.
    *   These keys must be configured as environment variables so that the `n8n` service can access them.

---

### 4. One.com (Production)

*   **Role:** DNS manager for the public domain.
*   **Usage:**
    *   In the production environment, the DNS settings for the domain `fgctrollhattan.se` (and subdomains like `checkin.` and `admin.`) point to the server where the Docker containers are running. This makes the system accessible over the internet.
*   **Requirements:**
    *   An account with One.com or another DNS provider where the domain is registered.
    *   Correctly configured A-records or CNAME-records.
