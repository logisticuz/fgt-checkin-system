# 3. Installation and Setup

This guide describes how to set up and run the project in a local development environment. The entire system is container-based with Docker, which significantly simplifies the installation process.

---

### Prerequisites

*   **Docker:** You must have Docker installed on your computer. [Download Docker Desktop](https://www.docker.com/products/docker-desktop/).
*   **Docker Compose:** This is usually included with Docker Desktop.
*   **Git:** To clone the project repository.
*   **A text editor:** E.g., Visual Studio Code.

---

### Step-by-step Guide

1.  **Clone the Project**
    Open a terminal or command prompt and run the following command to download the project files from GitHub:
    ```bash
    git clone <URL-to-your-git-repository>
    cd fgt-checkin-system
    ```

2.  **Configure Environment Variables (`.env`)**
    The system depends on a number of "secrets" and API keys. These should **not** be saved directly in the code.
    *   Find the file `.env.example` in the project's root directory.
    *   Create a copy of this file and name it `.env`.
    *   Open the new `.env` file and fill in all the required values. This includes:
        *   `AIRTABLE_API_KEY` and `AIRTABLE_BASE_ID` from your Airtable account.
        *   `STARTGG_API_KEY` from your Start.gg account.
        *   Other relevant keys and configurations.

3.  **Configure N8N (`n8n.env`)**
    The `n8n` service also needs its own environment variables, especially for setting up a username and password.
    *   Navigate to the `n8n/config/` directory.
    *   Find the file `n8n.env.example`.
    *   Create a copy and name it `n8n.env`.
    *   Open the new `n8n.env` file and fill in `N8N_BASIC_AUTH_USER` and `N8N_BASIC_AUTH_PASSWORD` to protect your n8n interface.

4.  **Build and Start the Services**
    Once the configuration files are in place, it's time to start the system. Make sure you are in the project's root directory in your terminal.
    *   Run the following command to build and start all services in development mode:
        ```bash
        docker-compose -f docker-compose.dev.yml up --build
        ```
    *   `--build` ensures that the Docker images are rebuilt if the code has changed.
    *   If you want to run the services in the background, you can add the `-d` flag:
        ```bash
        docker-compose -f docker-compose.dev.yml up -d --build
        ```

5.  **Verify the Installation**
    After the command has finished, all services should be running. You can now access the different parts of the system via your web browser:
    *   **Check-in Page:** [http://localhost](http://localhost)
        *   This is the public page that participants use. It is routed by Nginx to the `backend` service.
    *   **Admin Dashboard:** [http://localhost/admin/](http://localhost/admin/)
        *   This is the TOs' dashboard. It is routed by Nginx to the `fgt_dashboard` service.
    *   **N8N Interface:** [http://localhost:5678](http://localhost:5678)
        *   Here you can see and edit your n8n workflows. You will need to log in with the username and password you specified in `n8n.env`.

### Troubleshooting

*   **View logs:** If a service fails to start correctly, you can view its logs by running:
    ```bash
    # View all logs in real-time
    docker-compose -f docker-compose.dev.yml logs -f

    # View the log for a specific service (e.g., backend)
    docker-compose -f docker-compose.dev.yml logs -f backend
    ```
*   **Shut down the system:** To stop all services, run:
    ```bash
    docker-compose -f docker-compose.dev.yml down
    ```
