# FGT Check-in System - Dokumentation

Valkommen till den tekniska dokumentationen for FGT Check-in System. Detta dokument fungerar som en huvudsida och innehallsforteckning for att hjalpa utvecklare och tekniskt ansvariga att forsta, underhalla och vidareutveckla systemet.

Systemet ar designat for att automatisera och forenkla incheckningsprocessen for spelevent, vilket minskar manuellt arbete for arrangoerer och skapar en smidigare upplevelse for deltagare.

---

## Innehallsforteckning

Har ar en oversikt over de olika delarna av dokumentationen. Vi rekommenderar att du laser dem i ordning for att fa en komplett bild av projektet.

1.  **[Problem och Losning](./0_Problem_och_Losning.md)**
    *   Beskriver *varfor* detta projekt existerar. Vilka utmaningar loser det och vilka ar malen med systemet?

2.  **[Arkitektur](./1_Arkitektur.md)**
    *   En djupgaende titt pa systemets tekniska arkitektur, dess olika komponenter (`backend`, `fgt_dashboard`, `n8n`, `postgres`) och deras ansvarsomraden.

3.  **[Datafloden](./2_Datafloden.md)**
    *   Steg-for-steg-beskrivningar av de huvudsakliga processerna: hur en administrator konfigurerar ett event, hur en deltagare checkar in, och hur realtidsuppdateringar fungerar via SSE.

4.  **[Installation och Setup](./3_Installation_och_Setup.md)**
    *   En praktisk guide for att satta upp och kora projektet i en lokal utvecklingsmiljo med Docker.

5.  **[Externa Beroenden](./4_Externa_Beroenden.md)**
    *   En lista over de externa tjanster och API:er som systemet ar beroende av, sasom PostgreSQL, Start.gg och Sverok eBas.

6.  **[Forbattringsforslag](./5_Forbattringsforslag.md)**
    *   En sammanfattning av identifierade omraden dar systemet kan forbattras for att oka robusthet och underhallbarhet pa lang sikt.

7.  **[API Referens](./6_API_Referens.md)**
    *   Detaljerad beskrivning av systemets API-endpoints, deras anrop och forvantade svar.

8.  **[Datamodell](./7_Datamodell_Airtable.md)**
    *   En oversikt over schemat for de centrala databastabellerna (`settings`, `active_event_data`, `players`, `event_archive`, `event_stats`, `audit_log`).

9.  **[Frontend](./8_Frontend.md)**
    *   Beskriver frontendlogik, designsystem, validering, SSE-klient och Swish-integration.

10. **[Projektstatus](./PROJECT_STATUS.md)**
    *   Aktuell status, roadmap, senaste framsteg och nasta steg.

### Ovriga resurser

- **[Changelogs](./changelogs/)** — Detaljerade forandringsbeskrivningar per session (inklusive *varfor* beslut togs)
- **[Postgres Migration Plan](./postgres-migration-plan.md)** — Historisk plan for migrering fran Airtable till Postgres (genomford)
- **[Planned Features](./planned-feautures/)** — Specifikationer for planerade features (Swish-automatisering, Start.gg-registrering, etc.)
- **[Event History spec](./event-history/)** — Specifikation for event-arkivering och historik-funktionalitet
- **[Codex Feedback](./codex-feedback/)** — Arkitekturanalys och rekommendationer fran Codex
- **[Insights KPI spec](./insights-kpi-spec.md)** — Specifikation for Insights KPI-kort och metrik
