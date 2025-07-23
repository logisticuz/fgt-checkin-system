# FGT Check‑in System 🥊

*A lightweight, QR‑driven self‑check‑in platform for fighting‑game events.*

The goal is a **minimal‑viable** workflow that tournament organizers can spin up locally with Docker, while leaving the door open for future cloud deployments.

---

## ✨ Key Features (v0.2 MVP)

|  Flow                    |  What happens                                                                                                                  |  Notes                              |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------- |
| **QR check‑in**          | Player scans an event QR → lands on `checkin.html`, which POSTs an identifier.                                                 | Mobile or kiosk mode.               |
| **Smart status lookup**  | The webhook cross‑checks: • local CSV fall‑backs (Swish, Start.GG, eBas)• Airtable single source of truth                      | Adds API calls in later milestones. |
| **Dynamic registration** | If anything is missing (payment, licence ID, …) the user is redirected to `register.html` with only the needed fields visible. | JS toggles form sections.           |
| **Auto‑register**        | n8n writes data back to Airtable, logs an event and responds with a redirect.                                                  | Webhook → Airtable node.            |
| **TO dashboard (vNext)** | Airtable views for MVP → can evolve into Streamlit / Next.js dashboard.                                                        | Separate milestone.                 |

---

## 🏗️ High‑Level Architecture

```mermaid
graph TD
  subgraph User
    A[Mobile / Laptop]
  end
  subgraph Frontend
    B[checkin.html / register.html]<br/>(HTML + JS)
  end
  subgraph n8n[Backend – n8n]
    C[/checkin‑webhook\n/auto‑register/]
  end
  subgraph Data
    D[[CSV files]]
    E[[Airtable]]
  end
  subgraph Extra
    F[(Discord)]
  end

  A -- QR scan --> B
  B -- identifier --> C
  C -- look‑up --> D & E
  C -- status / redirect --> B
  B -- missing info --> C
  C -- update --> E
  C -- notify --> F
```

> **n8n** orchestrates webhooks, CSV/Airtable look‑ups, and (later) external APIs.
> **FastAPI** *(optional)* – serves HTML templates or extra REST endpoints when needed.
> **Docker Compose** bundles everything for quick local runs.

---

## 🚀 Quick‑start (local dev)

```bash
# 1 Clone & enter
   git clone https://github.com/logisticuz/fgt-checkin-system.git
   cd fgt-checkin-system

# 2 Copy env samples & add secrets
   cp .env.sample .env            # Airtable API key, etc.
   cp n8n/config/n8n.sample.env n8n/config/n8n.env

# 3 Boot the dev stack
   docker compose -f docker-compose.dev.yml up --build

# n8n     → http://localhost:5678
# FastAPI → http://localhost:8000 (if enabled)
```

*First‑time n8n?*  Complete the **owner setup** in your browser; import or rebuild the supplied flows.

---

## 🗺️ Road‑map

|  Milestone                                     |  Status        |
| ---------------------------------------------- | -------------- |
| CSV lookup, Airtable sync, basic pages (MVP)   | 🟣 in progress |
| Swap CSV for live Swish / Start.GG / eBas APIs | ⏳              |
| Streamlit / Next.js TO dashboard               | ⏳              |
| CI pipeline (lint + pytest + Docker build)     | ⏳              |
| Cloud deploy (Fly.io / Render)                 | ⏳              |

---

## 📫 Contact

Built with ❤️ by **Viktor Molina** ([@logisticuz](https://github.com/logisticuz)).
Questions or suggestions? Open an issue or ping us on Discord!
