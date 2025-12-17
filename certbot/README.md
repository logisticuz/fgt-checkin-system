# Certbot layout (placeholder)

Do not commit real certificates or private keys. This folder is here only to keep the volume structure in git.

Recommended layout (created at runtime by certbot):
- `conf/` – certbot config, accounts, live/archive, renewal files.
- `www/`  – webroot for HTTP-01 challenges.

Usage (example):
1. Start nginx with a location serving `certbot/www` at `/.well-known/acme-challenge/`.
2. Run (inside the certbot container or host):
   ```bash
   certbot certonly --webroot -w /var/www/certbot -d checkin.fgctrollhattan.se -d admin.fgctrollhattan.se
   ```
3. Renewal (cron/systemd/timer):
   ```bash
   certbot renew --webroot -w /var/www/certbot
   ```

Placeholders:
- `conf/.gitkeep`
- `www/.gitkeep`
