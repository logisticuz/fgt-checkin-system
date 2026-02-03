# Raspberry Pi 4 Migration Context

Detta dokument är för AI-agenter (Claude/Codex) på Raspberry Pi 4 för att förstå systemets kontext och migrationshistorik.

## Översikt

FGT Check-in System har migrerats från en Windows-utvecklingsdator till en dedikerad Raspberry Pi 4 för produktion.

**Arkitektur:**
- **Utveckling (DEV):** Windows-dator (denna repo ursprungligen)
- **Produktion (PROD):** Raspberry Pi 4 (ny host)

## Migrering (2026-02-03)

### Vad som gjordes

1. **Git clone** av repo till Pi4
2. **Kopierade `.env`** med alla API-nycklar
3. **Importerade n8n workflows** manuellt via UI (4 st JSON-filer)
4. **Skapade Airtable credentials** i n8n
5. **Pi-specifika anpassningar** - dokumentera nedan vad som ändrades

### Pi-specifika ändringar

> **VIKTIGT:** Dokumentera alla ändringar som gjordes för att få systemet att fungera på Pi4 här, så de kan mergas tillbaka till main.

```
# Lägg till ändringar här:
# - Fil: <sökväg>
#   Ändring: <beskrivning>
#   Anledning: <varför>
```

## Git-strategi

### Branch-struktur

```
main                 <- Stabil kod, synkad mellan DEV och PROD
  └── feature/*      <- Utveckling på Windows-datorn
  └── pi-hotfix/*    <- Akuta fixar direkt på Pi4
```

### Workflow

**Normal utveckling (Windows → Pi):**
1. Utveckla på Windows i `feature/*` branch
2. Testa lokalt med `docker compose -f docker-compose.dev.yml`
3. Merge till `main`
4. På Pi4: `git pull origin main` + `docker compose up -d --build`

**Hotfix på Pi4:**
1. Skapa branch: `git checkout -b pi-hotfix/beskrivning`
2. Gör fix
3. Commit + push: `git push -u origin pi-hotfix/beskrivning`
4. På Windows: `git pull` + merge till main
5. Eller: skapa PR på GitHub

**Synka Pi4 med main:**
```bash
cd /path/to/fgt-checkin-system
git fetch origin
git pull origin main
docker compose -f docker-compose.prod.yml up -d --build
```

## Viktiga filer

| Fil | Beskrivning |
|-----|-------------|
| `.env` | API-nycklar (INTE i git) |
| `docker-compose.prod.yml` | Prod-konfiguration |
| `nginx.conf` | Reverse proxy + SSL |
| `n8n/flows/*.json` | Workflow-backups (importeras manuellt) |

## n8n på Pi4

n8n workflows lagras i Docker volume `fgt-checkin-system_n8n_data`, inte i git.

**Workflows att ha aktiva:**
1. `FGC THN - Checkin Orchestrator v4` - Huvudflöde för check-in
2. `FGC THN - eBas Membership Check` - Medlemskapskontroll
3. `FGC THN - Start.gg Check` - Turneringsregistrering
4. `FGC THN - eBas Register` - Registrera nya medlemmar

**Vid workflow-ändringar:**
1. Exportera från n8n UI som JSON
2. Spara i `n8n/flows/`
3. Commit + push

## Kända Pi-specifika saker

- ARM64-arkitektur (alla Docker images måste stödja arm64)
- Begränsat RAM (4GB) - undvik tunga operationer
- SD-kort som storage - var försiktig med skrivningar

## Kontakt mellan miljöer

Om du (agent på Pi4) behöver information från Windows-utvecklingsmiljön:
1. Kolla senaste commits på `main` branch
2. Läs `docs/changelogs/` för sessionshistorik
3. Be användaren fråga Windows-agenten

## Senaste synk

**Datum:** 2026-02-03
**Commit:** 6b9fbff (feat: SSE token auth + n8n requirements fix + UI improvements)
**Status:** Pi4 körde med modifikationer - behöver dokumenteras ovan
