# FGT Check-in System 🥊

An automated, self-service check-in system for local gaming tournaments, designed to be run locally with Docker. It streamlines the check-in process by integrating with external services like Start.gg and Sverok eBas.

---

## 📚 Documentation

This project has extensive documentation available in both Swedish and English. For a complete understanding of the architecture, data flows, and setup, please refer to the documents in the `docs/` folders.

*   🇸🇪 **[View Swedish Documentation](./docs/README.md)**
*   🇬🇧 **[View English Documentation](./docs-en/README.md)**

---

## ✨ Key Features

*   **Automated Check-in:** Participants can check themselves in via a simple web form. The system automatically verifies:
    *   Membership status via the Sverok eBas API.
    *   Tournament registration via the Start.gg API.
    *   Payment status.
*   **Real-time Admin Dashboard:** A comprehensive dashboard built with Plotly Dash allows Tournament Organizers (TOs) to monitor check-in status in real-time, configure the active event, and see which participants need assistance.
*   **Dynamic Registration Flow:** If a participant is missing any requirements, they are automatically guided to a page where they can complete the necessary steps.
*   **Microservice Architecture:** The system is fully containerized using Docker and consists of several independent services, including a FastAPI backend, a Dash dashboard, and an n8n instance for workflow automation.

---

## 🚀 Quick-start (Local Development)

The installation instructions are still valid. Make sure you have Docker installed.

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
    docker-compose -f docker-compose.dev.yml up --build
    ```

4.  **Access the services:**
    *   **Check-in Page:** [http://localhost](http://localhost)
    *   **Admin Dashboard:** [http://localhost/admin/](http://localhost/admin/)
    *   **N8N Interface:** [http://localhost:5678](http://localhost:5678)

---

## 🗺️ Roadmap & Tasks

The project's roadmap and a list of pending tasks are maintained in the following files. Please refer to them for planned improvements and future features.

*   🇸🇪 **[TODOLIST.md](./TODOLIST.md)** (Swedish)
*   🇬🇧 **[TASKS.md](./TASKS.md)** (English)

---

## 📫 Contact

Built with ❤️ by **Viktor Molina** ([@logisticuz](https://github.com/logisticuz)).
Questions or suggestions? Open an issue or ping us on Discord!