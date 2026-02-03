# Arbetsorder: SSL-certifikat för produktion

## Bakgrund
Systemet kör på en Raspberry Pi med Docker Compose. Alla tjänster är uppe och fungerar över HTTP, men SSL-certifikat saknas för HTTPS.

## Domäner
- `admin.fgctrollhattan.se` - Dashboard/admin-gränssnitt
- `checkin.fgctrollhattan.se` - Checkin-gränssnitt

## Nuläge
- nginx körs och lyssnar på port 80 och 443
- certbot-container finns men inga certifikat är genererade
- Mapp för certifikat: `./certbot/conf/` (tom)
- Mapp för webroot: `./certbot/www/`

## Uppgift
Konfigurera SSL med Let's Encrypt för båda domänerna.

### Steg 1: Uppdatera nginx.conf
Lägg till en location för ACME-challenge i varje server block (port 80) så certbot kan verifiera domänerna:

```nginx
location /.well-known/acme-challenge/ {
    root /var/www/certbot;
}
```

### Steg 2: Generera certifikat
Kör certbot i webroot-läge för båda domänerna (kan göras i ett kommando):

```bash
docker compose -f docker-compose.prod.yml run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d admin.fgctrollhattan.se \
  -d checkin.fgctrollhattan.se \
  --email <EMAIL> \
  --agree-tos \
  --no-eff-email
```

Alternativt separat per domän:

```bash
# Admin
docker compose -f docker-compose.prod.yml run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d admin.fgctrollhattan.se \
  --email <EMAIL> \
  --agree-tos \
  --no-eff-email

# Checkin
docker compose -f docker-compose.prod.yml run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d checkin.fgctrollhattan.se \
  --email <EMAIL> \
  --agree-tos \
  --no-eff-email
```

### Steg 3: Uppdatera nginx.conf för HTTPS
Lägg till SSL server blocks för båda domänerna:

```nginx
# Admin dashboard
server {
    listen 443 ssl;
    server_name admin.fgctrollhattan.se;

    ssl_certificate /etc/letsencrypt/live/admin.fgctrollhattan.se/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.fgctrollhattan.se/privkey.pem;

    location / {
        proxy_pass http://fgt_dashboard:8050;
        # ... övrig proxy-konfiguration ...
    }
}

# Checkin frontend
server {
    listen 443 ssl;
    server_name checkin.fgctrollhattan.se;

    ssl_certificate /etc/letsencrypt/live/checkin.fgctrollhattan.se/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/checkin.fgctrollhattan.se/privkey.pem;

    location / {
        proxy_pass http://backend:8000;
        # ... övrig proxy-konfiguration ...
    }
}
```

### Steg 4: HTTP → HTTPS redirect
Uppdatera port 80 server blocks att redirecta till HTTPS:

```nginx
server {
    listen 80;
    server_name admin.fgctrollhattan.se checkin.fgctrollhattan.se;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
```

### Steg 5: Auto-renewal
Lägg till cron-jobb för automatisk förnyelse (kör t.ex. varje vecka):

```bash
0 3 * * 0 docker compose -f /home/lgz-pi/fgt-checkin-system/docker-compose.prod.yml run --rm certbot renew && docker compose -f /home/lgz-pi/fgt-checkin-system/docker-compose.prod.yml exec nginx nginx -s reload
```

## Filer att ändra
- `nginx.conf` - Lägg till ACME-location, SSL server blocks, och HTTPS redirect
- Eventuellt `docker-compose.prod.yml` om certbot behöver justeras

## Krav
- Båda domänerna måste peka på serverns publika IP (DNS A-record)
- Port 80 måste vara öppen från internet för verifiering
- E-postadress för Let's Encrypt notifikationer

## Verifiering
Efter implementation, verifiera med:
```bash
curl -I https://admin.fgctrollhattan.se
curl -I https://checkin.fgctrollhattan.se
```
