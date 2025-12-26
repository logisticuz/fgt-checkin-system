# 1. Architecture

The system is designed with a modern **microservices-inspired architecture**. This means the application is divided into several smaller, independent services that communicate with each other over a network. All services run in their own Docker containers, making the system portable and easy to deploy.

Here is an overview of the primary components in the system:

![Architecture Diagram](https://i.imgur.com/r8sH3jU.png)
*(This is a simplified diagram to illustrate the flow between the main components)*

---

### Components

#### 1. Backend (`backend/`)
*   **Technology:** FastAPI (Python)
*   **Responsibility:** This is the hub for the public-facing application and the real-time data flow.
    *   **Web Server:** Serves the HTML pages that the user sees, e.g., the check-in form (`checkin.html`) and the status pages.
    *   **N8N Proxy:** All traffic from the user's browser to `n8n` is routed through a proxy endpoint (`/n8n/...`) in this service. This is an important security feature that hides the internal `n8n` service. The proxy also intercepts incoming data, validates and "sanitizes" it using `validation.py` before forwarding it.
    *   **Status API:** Provides `GET /api/participant/{name}/status` which reads participant status directly from Airtable. This endpoint is used by `status_pending.html` for polling.
    *   **SSE Hub:** Manages Server-Sent Events (`GET /api/events/stream`) for real-time dashboard updates. It also exposes `/api/notify/checkin` and `/api/notify/update`, which `n8n` calls to send notifications to connected SSE clients. This allows the backend to act as a bridge for real-time flows between `n8n` and the `fgt_dashboard`.

#### 2. FGT Dashboard (`fgt_dashboard/`)
*   **Technology:** Plotly Dash mounted inside a FastAPI app.
*   **Responsibility:** This is the primary tool for the Tournament Organizers (TOs).
    *   **Administrative Interface:** Provides a web interface (available at `/admin/`) where TOs can manage and monitor events.
    *   **Event Configuration:** The most critical function is fetching tournament data. A TO pastes a link from Start.gg, and the dashboard calls Start.gg's GraphQL API to retrieve all relevant details (event, participants, etc.). This information is then saved to the `settings` table in Airtable.
    *   **Real-time Overview:** Displays a live-updated table with all checked-in participants and their status (Green, Red, etc.), received via **Server-Sent Events (SSE)**. It also has a "Needs Attention" section to quickly identify who needs help.

#### 3. N8N (`n8n/`)
*   **Technology:** n8n.io (Workflow Automation)
*   **Responsibility:** This is the "brain" of the system that executes the actual check-in logic and coordinates external API calls.
    *   **Workflows:** `n8n` listens for webhooks that are called by the `backend` service.
    *   **Business Logic:** When a `checkin` request arrives, `n8n` executes a workflow that performs the following steps:
        1.  Checks the participant's membership via the **Sverok eBas API**.
        2.  Checks the participant's tournament registration via the **Start.gg API** (based on the `tag`).
        3.  Checks the payment status (by reading from Airtable, no external API integration here).
        4.  Updates the participant's row in the `active_event_data` table in Airtable with the result.
        5.  **Calls the `backend` service's `/api/notify/checkin` (or `/api/notify/update`)** to trigger a real-time update via SSE to the dashboard.
        6.  Returns a `JSON` response to the `backend` indicating if the check-in was successful or what is missing.

#### 4. Nginx
*   **Technology:** Nginx
*   **Responsibility:** Acts as a **reverse proxy** and the system's single public entry point.
    *   **Traffic Routing:** Receives all incoming web traffic and directs it to the correct internal service based on the URL.
        *   Requests to `domain.com/admin/...` are sent to the `fgt_dashboard`.
        *   All other requests (`domain.com/...`) are sent to the `backend`.
    *   **SSL Termination:** In the production environment, Nginx handles HTTPS and SSL certificates (via Certbot) to ensure encrypted traffic.

#### 5. Airtable
*   **Technology:** Airtable (Cloud Database)
*   **Responsibility:** Serves as the system's central database.
    *   **`settings`:** A table that contains the configuration for the current active event. This data is written by the `fgt_dashboard`.
    *   **`active_event_data`:** A table that acts as a live database for all check-ins during an event. This data is primarily written by `n8n` and read by both the `backend` and `fgt_dashboard`.
