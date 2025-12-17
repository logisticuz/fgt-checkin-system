# FGT Check-in System - Documentation

Welcome to the technical documentation for the FGT Check-in System. This document serves as a main page and table of contents to help developers and technical managers understand, maintain, and further develop the system.

The system is designed to automate and simplify the check-in process for gaming events, reducing manual work for organizers and creating a smoother experience for participants.

---

## Table of Contents

Here is an overview of the different parts of the documentation. We recommend reading them in order to get a complete picture of the project.

1.  **[Problem and Solution](./0_Problem_and_Solution.md)**
    *   Describes *why* this project exists. What challenges does it solve, and what are the goals of the system?

2.  **[Architecture](./1_Architecture.md)**
    *   A deep dive into the system's technical architecture, its various components (`backend`, `fgt_dashboard`, `n8n`), and their responsibilities.

3.  **[Data Flows](./2_Data_Flow.md)**
    *   Step-by-step descriptions of the two main processes: how an administrator configures an event and how a participant checks in.

4.  **[Installation and Setup](./3_Installation_and_Setup.md)**
    *   A practical guide to setting up and running the project in a local development environment with Docker.

5.  **[External Dependencies](./4_External_Dependencies.md)**
    *   A list of the external services and APIs that the system depends on, such as Airtable, Start.gg, and Sverok eBas.

6.  **[Improvement Suggestions](./5_Improvement_Suggestions.md)**
    *   A summary of identified areas where the system can be improved to increase robustness and long-term maintainability.

7.  **[API Reference](./6_API_Reference.md)**
    *   Detailed description of the system's API endpoints and n8n webhooks, their requests, and expected responses.

8.  **[Data Model - Airtable](./7_Data_Model_Airtable.md)**
    *   An overview of the schema for the central Airtable tables (`settings`, `active_event_data`), including fields, data types, and purpose.