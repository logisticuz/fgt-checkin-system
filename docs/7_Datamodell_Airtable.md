# 7. Datamodell - Airtable

Airtable fungerar som den centrala databasen för systemet, där all viktig information om eventinställningar och deltagarstatus lagras. Denna dokumentation beskriver schemat för de viktigaste tabellerna.

---

## 1. Tabell: `settings`

*   **Syfte:** Lagrar den övergripande konfigurationen för det **aktuella aktiva eventet**. Denna rad uppdateras av **FGT Dashboard** när en turneringsorganisatör konfigurerar ett event.
*   **Viktiga Fält:**

| Fältnamn                  | Typ              | Beskrivning                                                              | Exempel                           |
| :------------------------ | :--------------- | :----------------------------------------------------------------------- | :-------------------------------- |
| `Name`                    | `Single line text` | Namnet på inställningsraden (ofta bara "Active Settings").               | `Active Settings`                 |
| `is_active`               | `Checkbox`       | **MÅSTE VARA BOCKAD** för att systemet ska känna igen raden som den aktiva konfigurationen. | `[x]` (bockad)                    |
| `active_event_slug`       | `Single line text` | Den unika "sluggen" från Start.gg för den aktiva turneringen.           | `fgc-trollhattan-weekly-42`       |
| `event_date`              | `Date`           | Startdatumet för turneringen (hämtas från Start.gg).                      | `2025-12-17`                      |
| `default_game`            | `Multi-select`   | Lista över spel som är valda av TO att visas i formuläret. Fylls från Start.gg events. | `["Street Fighter 6", "Tekken 8"]` |
| `events_json`             | `Long text`      | Rå `JSON`-data innehållande all information om eventen från Start.gg. Används för att bygga `default_game`-listan. | `{"events": [...]}`               |
| `startgg_event_url`       | `URL`            | Den ursprungliga URL:en till Start.gg-turneringen.                      | `https://start.gg/tournament/...` |
| `tournament_name`         | `Single line text` | Namnet på turneringen (från Start.gg).                                  | `FGC Trollhättan Weekly #42`      |
| `timezone`                | `Single line text` | Tidzonen för turneringen (från Start.gg).                               | `Europe/Stockholm`                |
| `swish_number`            | `Single line text` | Swish-nummer för betalningar (om använt).                                | `1234567890`                      |
| `swish_expected_per_game` | `Number`         | Förväntad Swish-betalning per spel (i SEK).                             | `50`                              |

---

## 2. Tabell: `active_event_data`

*   **Syfte:** Lagrar **live-data** för varje deltagares incheckning för det aktiva eventet. Denna tabell uppdateras primärt av **N8N** och läses av både **Backend** och **FGT Dashboard**.
*   **Viktig underhållsnotering:** Funktionen `get_checkins` i `shared/airtable_api.py`, som läser från denna tabell, använder en statisk fältlista. Om ett nytt fält läggs till i denna tabell i Airtable måste det även läggas till manuellt i fältlistan i den funktionen för att datan ska hämtas.
*   **Viktiga Fält:**

| Fältnamn                      | Typ              | Beskrivning                                                              | Exempel                                    |
| :---------------------------- | :--------------- | :----------------------------------------------------------------------- | :----------------------------------------- |
| `name`                        | `Single line text` | Deltagarens fullständiga namn (tvättat).                                 | `Anna Andersson`                           |
| `tag`                         | `Single line text` | Deltagarens gamer-tag (tvättat). Används för matchning mot Start.gg.      | `CoolPlayer`                               |
| `email`                       | `Email`          | Deltagarens e-postadress.                                                | `anna@example.com`                         |
| `telephone`                   | `Phone number`   | Deltagarens telefonnummer (tvättat, endast siffror).                     | `0701234567`                               |
| `personnummer`                | `Single line text` | Deltagarens personnummer (tvättat, endast siffror). Används för matchning mot Sverok. **Lagras ej permanent**. | `199001011234`                             |
| `UUID`                        | `Single line text` | En unik identifierare för denna incheckning (genereras av n8n).          | `b7d4e1f8-c2a7-...`                         |
| `external_id`                 | `Single line text` | Extern ID, t.ex. från Start.gg-registrering.                             | `startgg_reg_12345`                        |
| `event_slug`                  | `Single line text` | Start.gg-sluggen för eventet deltagaren checkar in till.                 | `fgc-trollhattan-weekly-42`                |
| `startgg_event_id`            | `Single line text` | ID för det specifika Start.gg-eventet (t.ex. för ett visst spel).         | `123456`                                   |
| `tournament_games_registered` | `Multi-select`   | Lista över spel som deltagaren är registrerad för i turneringen.          | `["Street Fighter 6", "Tekken 8"]`         |
| `member`                      | `Checkbox`       | `[x]` om deltagaren är verifierad medlem i föreningen.                   | `[x]`                                      |
| `startgg`                     | `Checkbox`       | `[x]` om deltagaren är verifierad registrerad på Start.gg för eventet.   | `[x]`                                      |
| `is_guest`                    | `Checkbox`       | `[x]` om spelaren **inte** hittades på Start.gg. Sätts automatiskt.      | `[x]`                                      |
| `payment_amount`              | `Number`         | Belopp som deltagaren har betalat (från Swish-matchning eller manuellt). | `100`                                      |
| `payment_expected`            | `Number`         | Förväntat betalningsbelopp.                                              | `100`                                      |
| `payment_valid`               | `Checkbox`       | `[x]` om betalningen har verifierats och är korrekt.                     | `[x]`                                      |
| `status`                      | `Single select`  | Övergripande status för deltagaren: `Ready`, `Pending`.                  | `Ready`                                    |
| `created`                     | `Created time`   | Tidstämpel för när incheckningsraden skapades. **Notering:** Detta är ett metadata-fält från Airtable (`createdTime`) och kan inte redigeras manuellt. | `2025-12-17T11:30:00.000Z`                 |

---

## 3. Tabell: `players` (Potentiell)

*   **Syfte:** Denna tabell kan användas som en masterlista över alla spelare, oberoende av event. För närvarande används den sparsamt eller inte alls i de primära flödena.
*   **Viktiga Fält:** Kan inkludera `name`, `email`, `tag`, `telephone`, `created`.

---

## 4. Tabell: `event_history` och `event_history_dashboard`

*   **Syfte:** Dessa tabeller är tänkta att lagra arkiverad eller denormaliserad historisk data om event för statistik eller snabbare läsning för dashboarden. Det exakta innehållet och hur de fylls är inte fullständigt definierat i de primära flödena, men de indikerar ett behov av historisk data.
*   **Viktiga Fält:** Troligtvis `event_slug`, `status`, `participants`, `created`.

---

## 5. Namnkonventioner

### Fältnamn: `tag` (inte `gametag` eller `gamerTag`)

Systemet använder **`tag`** som standardnamn för spelarens "gamer-tag". Detta är konsekvent över hela kodbasen för att undvika förvirring. Namnet `tag` är kort, universellt och fungerar i alla tekniska kontexter (Python, JS, etc.).
