FGT Check‑in System 🏷️

A lightweight, QR‑driven self‑check‑in platform for fighting‑game events.

The goal is a minimal‑viable workflow that TOs can spin up locally with Docker while leaving plenty of head‑room for future automation, analytics, and third‑party integrations.

✨ Key Features (v0.2 MVP)

Flow

What happens

Notes

QR check‑in

Player scans the event QR and lands on checkin.html, which POSTs an identifier (email / phone / nick) to an n8n webhook.

No login or app installation required.

Smart status lookup

The webhook cross‑checks:• local CSV fall‑backs (Swish, Start.gg, eBas)• Airtable “source of truth”.

CSV stubs make offline testing easy; later swapped for live APIs.

Dynamic registration

If anything is missing (payment, licence ID, …) the user is redirected to register.html; the form only shows the fields that still need input.

Form POSTs to /auto-register (n8n).

Auto‑register

n8n writes data back to Airtable, logs an event, then responds with a redirect to status_ready.html or status_pending.html.

Optional Discord webhook for crew notifications.

TO dashboard (vNext)

Airtable views for MVP → can evolve into a Streamlit / Next.js dashboard.



🗺️ High‑Level Architecture

User (mobile/laptop)
        │ 1 QR scan
Frontend (HTML + JS) ──▶ 2 / checkin‑webhook ──▶ n8n
                       ▲                       │
       redirect /status│                       │
register.html  ──▶ 6 / auto‑register ──▶ Airtable
                       │                       ▲
                       └────► Discord (optional)

n8n – orchestrates webhooks, CSV/Airtable look‑ups, and (later) external APIs.

FastAPI (optional) – serves HTML templates or extra REST endpoints when needed.

Docker Compose – bundles everything for quick local runs.

📂 Repo Layout

.
├── backend/              # optional FastAPI app
│   ├── data/             # CSV stubs (ebas, startgg, swish…)
│   ├── templates/        # checkin / register / status pages
│   └── main.py
├── n8n/config/           # n8n.env  +  starter workflows
├── docker-compose.dev.yml
├── docker-compose.prod.yml
└── README.md

🚀 Quick Start

# 1 Clone
$ git clone https://github.com/logisticuz/fgt-checkin-system.git
$ cd fgt-checkin-system

# 2 Secrets
$ cp .env.example .env                   # Airtable API key, etc.
$ cp n8n/config/n8n.sample.env n8n/config/n8n.env

# 3 Boot the dev stack
$ docker compose -f docker-compose.dev.yml up --build

# n8n      → http://localhost:5678
# FastAPI  → http://localhost:8000  (if enabled)


🗓️ Road‑map

Milestone

Status

CSV lookup, Airtable sync, basic pages (MVP)

✔ in progress

Swap CSV for live Swish / Start.gg / eBas APIs

⏳

Streamlit / Next.js TO dashboard

⏳

CI pipeline (lint + pytest + Docker build)

⏳

Cloud deploy (Fly.io / Render)

⏳


🙋 Contact

Built with ❤️ by Viktor Molina (@logisticuz) 
