# Repository Guidelines

## Project Structure & Module Organization
- `backend/`: FastAPI service that serves templates, proxies n8n webhooks, and exposes APIs.
- `fgt_dashboard/`: Plotly Dash admin UI and assets (SSE client, styles).
- `shared/`: Shared Python modules, including Airtable access.
- `n8n/flows/`: Exported workflow JSON backups.
- `nginx/`: Reverse proxy configuration.
- `docs/` and `docs-en/`: Architecture, data flow, API, and model docs.
- `docs/changelogs/` and `docs/todos/`: Session history and task tracking.

## Build, Test, and Development Commands
- `docker compose -f docker-compose.dev.yml up --build`: Start the dev stack with hot reload.
- `docker compose -f docker-compose.dev.yml logs -f backend`: Tail backend logs.
- `docker compose -f docker-compose.dev.yml down`: Stop all services.
- `docker compose -f docker-compose.prod.yml up -d --build fgt_dashboard`: Rebuild a prod service.
- `black --line-length 100 .` and `isort --profile black --line-length 100 .`: Format Python code.

## Coding Style & Naming Conventions
- Python uses 4-space indentation; format with Black/isort (100 columns).
- Use `tag` for player tags (not `gametag` or `gamerTag`).
- Keep Airtable field names consistent with the schema in `docs/7_Datamodell_Airtable.md`.
- Language: communicate in Swedish; code, identifiers, and comments stay in English.

## Testing Guidelines
- No dedicated automated test suite is defined yet.
- Validate changes manually via the check-in flow and dashboard, and monitor logs.
- If you add tests, document how to run them here.

## Commit & Pull Request Guidelines
- Use Conventional Commits (e.g., `feat: ...`, `fix: ...`, `chore: ...`, optional scopes like `feat(dashboard): ...`).
- PRs should include: a short summary, testing notes, and screenshots for UI changes.
- Update `docs/changelogs/` when making significant changes.

## Security & Configuration Tips
- Do not commit secrets. Use `.env` and `n8n/config/n8n.env` (copy from examples).
- If adding Airtable fields, also update the field list in `shared/airtable_api.py`.
- Protect webhooks via `N8N_WEBHOOK_TOKEN` when enabled.

## Architecture Overview
- The system runs as Dockerized services: backend, dashboard, n8n, nginx, and Airtable as the data store.

## Agent-Specific Instructions
- Workflow: Claude writes code and the session changelog, Gemini updates documentation, and Codex handles Git operations.
- Perform commits/updates only when requested, and base them on the latest entry in `docs/changelogs/`.
- Environment workflow:
  - Day-to-day development happens on the separate dev computer.
  - The Raspberry Pi is the production host and should normally receive changes by pulling from Git.
  - Sometimes urgent production hotfixes are made directly on the Raspberry Pi.
  - When a production hotfix changes application code, reverse-proxy config, or operational docs, preserve it in Git so the dev computer can pull it back and dev/prod do not diverge.
  - Do not commit runtime data, generated backups, secrets, certbot live/archive material, or local crontab state.
