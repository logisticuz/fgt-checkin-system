# Tests

Install `requirements-dev.txt` into a dedicated virtual environment, then run from the repo root:

```powershell
python -m pytest -p no:cacheprovider -q
python -m pytest -p no:cacheprovider -m "not db" -q
```

The conductor owns the disposable Postgres container at `127.0.0.1:15432`.
`TEST_DATABASE_URL` defaults to `postgresql://fgt_test:fgt_test@127.0.0.1:15432/fgt_test`.
Only that port on `127.0.0.1` or `localhost` is accepted, including URI query overrides.
Other destinations abort before collection; `TEST_DATABASE_ALLOW_ANY=1` is an explicit override
for another disposable database. Never use it for shared DEV or PROD databases.
Unreachable Postgres skips DB tests with `test Postgres unreachable`.

`pg_db` creates a unique schema per session, applies `db/init.sql` and the real migrations,
empties its tables before each requesting test, and drops the schema at session teardown.
`app_client` uses real FastAPI handlers and seeds synthetic active-event settings.
`handler_client` covers handlers that do not require Postgres.
The application runs with `INTEGRATION_ENGINE=none`; its current unconditional n8n call is stubbed.
`n8n_stub`, `ebas_stub`, `startgg_stub`, and `smtp_stub` record calls and accept explicit responses.
Real httpx, requests, aiohttp, and SMTP network calls raise immediately.
Inherited provider credentials and libpq connection defaults are removed during the session.

**No real data, credentials, personal IDs, phone numbers, or external writes in tests.**
Use `tests.fixtures.make_settings()` and `make_checkin()` for invented data.
Strict expected failures document known application bugs owned by later tasks.
