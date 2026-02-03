# Git Sync Rutin: Windows ↔ GitHub ↔ Pi4

## Översikt

```
┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│  Windows-dator  │ ──push──│     GitHub      │──pull── │  Raspberry Pi4  │
│      (DEV)      │ ◄─pull──│ (source of truth)│◄─push── │     (PROD)      │
└─────────────────┘         └─────────────────┘         └─────────────────┘
```

**GitHub är alltid sanningen.** Båda maskiner synkar mot GitHub, aldrig direkt mot varandra.

---

## Daglig rutin

### När du utvecklat klart på Windows:

```bash
# 1. Kolla vad som ändrats
git status

# 2. Stagea och committa
git add -A
git commit -m "beskrivning av ändringen"

# 3. Pusha till GitHub
git push origin main
```

### När Pi4 ska uppdateras (inför turnering etc):

**På Pi4:**
```bash
cd ~/fgt-checkin-system   # eller var repot ligger

# 1. Hämta senaste från GitHub
git pull origin main

# 2. Bygg om containers
docker compose -f docker-compose.prod.yml up -d --build
```

---

## Om du fixar något direkt på Pi4

Ibland måste du göra en snabb fix i prod. Då:

**På Pi4:**
```bash
# 1. Gör din fix
nano nginx.conf  # eller vilken fil

# 2. Committa
git add -A
git commit -m "fix: beskrivning"

# 3. Pusha till GitHub
git push origin main
```

**På Windows efteråt:**
```bash
# Hämta Pi4-ändringarna
git pull origin main
```

---

## Checklista innan turnering

- [ ] Windows: `git push origin main` (pusha alla ändringar)
- [ ] Pi4: `git pull origin main` (hämta senaste)
- [ ] Pi4: `docker compose -f docker-compose.prod.yml up -d --build`
- [ ] Testa: öppna https://www.checkin.fgctrollhattan.se
- [ ] Testa: öppna https://admin.fgctrollhattan.se

---

## Vanliga kommandon

| Vad | Kommando |
|-----|----------|
| Se status | `git status` |
| Se vad som ändrats | `git diff` |
| Hämta utan att merga | `git fetch origin main` |
| Se senaste commits | `git log --oneline -5` |
| Ångra lokala ändringar | `git checkout -- filnamn` |
| Se remote-status | `git log --oneline origin/main -5` |

---

## Om det blir konflikt

Om både Windows och Pi4 ändrat samma fil:

```bash
# 1. Försök pulla
git pull origin main

# 2. Om konflikt - öppna filen och fixa manuellt
#    Leta efter <<<<<<< och >>>>>>> markeringar

# 3. Efter fix
git add filnamn
git commit -m "fix: resolve merge conflict"
git push origin main
```

---

## Filer som INTE ska pushas

Dessa är i `.gitignore`:

- `.env` - API-nycklar (kopiera manuellt mellan maskiner)
- `nginx/htpasswd` - lösenord (skapa lokalt på varje maskin)
- `n8n_data/` - n8n databas (Docker volume)
- `certbot/` - SSL-certifikat (maskinspecifika)

---

## Quick reference

```bash
# === WINDOWS (efter kodning) ===
git add -A && git commit -m "beskrivning" && git push

# === PI4 (uppdatera prod) ===
git pull origin main && docker compose -f docker-compose.prod.yml up -d --build

# === WINDOWS (hämta Pi4-ändringar) ===
git pull origin main
```
