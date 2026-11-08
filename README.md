# FGT Check-in System

An automated, self-service check-in system for local gaming tournaments, designed to be run locally with Docker. It streamlines the check-in process by integrating with external services like Start.gg and Sverok eBas.

---

## Documentation

This project has extensive documentation available in both Swedish and English. For a complete understanding of the architecture, data flows, and setup, please refer to the documents in the `docs/` folders.

*   **[View Swedish Documentation](./docs/README.md)**
*   **[View English Documentation](./docs-en/README.md)**

---

## Key Features

*   **Automated Check-in:** Participants can check themselves in via a simple web form. The system automatically verifies:
    *   Membership status via the Sverok eBas API.
    *   Tournament registration via the Start.gg API.
    *   Payment status.
*   **Real-time Admin Dashboard:** A comprehensive dashboard built with Plotly Dash allows Tournament Organizers (TOs) to monitor check-in status in real-time, configure the active event, and see which participants need assistance.
*   **Dynamic Registration Flow:** If a participant is missing any requirements, they are automatically guided to a page where they can complete the necessary steps.
*   **Microservice Architecture:** The system is fully containerized using Docker and consists of several independent services, including a FastAPI backend, a Dash dashboard, an n8n integration engine, and a PostgreSQL database.

---

## Tech Stack

*   **Backend:** Python, FastAPI, Uvicorn
*   **Dashboard:** Python, Dash, Plotly, Pandas
*   **Database:** PostgreSQL (primary), Airtable (legacy fallback)
*   **Integration Engine:** n8n (external API calls only)
*   **Containerization:** Docker, Docker Compose
*   **Reverse Proxy:** Nginx (SSL, rate limiting)
*   **External APIs:** Start.gg (GraphQL), Sverok eBas (REST)

---

## Quick-start (Local Development)

Make sure you have Docker installed.

1.  **Clone & enter repository**
    ```bash
    git clone https://github.com/logisticuz/fgt-checkin-system.git
    cd fgt-checkin-system
    ```

2.  **Copy env samples & add secrets**
    ```bash
    cp .env.example .env
    cp n8n/config/n8n.env.example n8n/config/n8n.env
    ```
    *Edit `.env` and `n8n/config/n8n.env` with your API keys and credentials.*

3.  **Boot the dev stack**
    ```bash
    docker compose -p fgt-dev -f docker-compose.dev.yml up --build
    ```

4.  **Access the services:**
    *   **Check-in Page:** [http://localhost:8088](http://localhost:8088)
    *   **Admin Dashboard:** [http://localhost:8088/admin/](http://localhost:8088/admin/)
    *   **n8n Interface:** [http://localhost:5679](http://localhost:5679)

---

## FightBox DEV rehearsal

Run `python scripts/sim/fightbox_rehearsal.py --restore-after` from the build worktree on
the Windows Docker host. The runner takes the agent-hub `dev-db-shared` lock, backs up DEV
Postgres and the stopped n8n volume beneath `C:\Users\vikto\agent-hub\backups`, then sends
25 synthetic attempts through DEV nginx from 16 container IPs (five people share one IP).
It uses legacy registration, private eBas/Start.gg HTTP simulators, generated workflow
copies and dummy provider keys. No simulator binds a host port. Published workflow URLs
and keys must pass the guard before the first check-in. Reports contain synthetic roles only.
Every check is named in the report; injected scenario waits are measured separately from
the remaining HTTP/backend/n8n time. Only the shipped synthetic identity catalog is accepted.

The mock event writes `event_date=2026-11-14`, a free price, membership required,
Start.gg admission optional and games SSBU/SF6/T8/Strive. It confirms a duplicate, archives,
reopens, adds two manual backfills and checks arrival-time preservation and fresh statistics.
Default mode snapshots, runs and reports; n8n/runtime always restore. `--restore-after`
also restores Postgres before releasing the lock. `--snapshot` only creates backups;
`--restore <backup-directory>/postgres.dump` explicitly restores both data stores.
`--target` accepts only localhost/127.0.0.1:8088; `--duration` and `--seed` control the rush.
The default compose source is sibling `wt/devstack`; its files are read and never edited.

## Contact

Built by **Viktor Molina** ([@logisticuz](https://github.com/logisticuz)).
Questions or suggestions? Open an issue or ping us on Discord!
