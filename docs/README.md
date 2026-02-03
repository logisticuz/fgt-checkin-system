# FGT Check-in System - Dokumentation

Välkommen till den tekniska dokumentationen för FGT Check-in System. Detta dokument fungerar som en huvudsida och innehållsförteckning för att hjälpa utvecklare och tekniskt ansvariga att förstå, underhålla och vidareutveckla systemet.

Systemet är designat för att automatisera och förenkla incheckningsprocessen för spelevent, vilket minskar manuellt arbete för arrangörer och skapar en smidigare upplevelse for deltagare.

---

## Innehållsförteckning

Här är en översikt över de olika delarna av dokumentationen. Vi rekommenderar att du läser dem i ordning för att få en komplett bild av projektet.

1.  **[Problem och Lösning](./0_Problem_och_Losning.md)**
    *   Beskriver *varför* detta projekt existerar. Vilka utmaningar löser det och vilka är målen med systemet?

2.  **[Arkitektur](./1_Arkitektur.md)**
    *   En djupgående titt på systemets tekniska arkitektur, dess olika komponenter (`backend`, `fgt_dashboard`, `n8n`) och deras ansvarsområden.

3.  **[Dataflöden](./2_Datafloden.md)**
    *   Steg-för-steg-beskrivningar av de tre huvudsakliga processerna: hur en administratör konfigurerar ett event, hur en deltagare checkar in, och hur status-polling fungerar för väntande deltagare.

4.  **[Installation och Setup](./3_Installation_och_Setup.md)**
    *   En praktisk guide för att sätta upp och köra projektet i en lokal utvecklingsmiljö med Docker.

5.  **[Externa Beroenden](./4_Externa_Beroenden.md)**
    *   En lista över de externa tjänster och API:er som systemet är beroende av, såsom Airtable, Start.gg och Sverok eBas.

6.  **[Förbättringsförslag](./5_Forbattringsforslag.md)**
    *   En sammanfattning av identifierade områden där systemet kan förbättras för att öka robusthet och underhållbarhet på lång sikt.

7.  **[API Referens](./6_API_Referens.md)**
    *   Detaljerad beskrivning av systemets API-endpoints och n8n-webhooks, deras anrop och förväntade svar.

8.  **[Datamodell - Airtable](./7_Datamodell_Airtable.md)**
    *   En översikt över schemat för de centrala Airtable-tabellerna (`settings`, `active_event_data`), inklusive fält, datatyper och syfte.
