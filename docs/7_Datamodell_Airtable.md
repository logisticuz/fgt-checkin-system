# 7. Datamodell

> **Postgres ar systemets primara databas** (sedan 2026-02). Det auktoritativa schemat finns i `db/init.sql` och `shared/postgres_api.py`. Airtable stods fortfarande som legacy fallback via `DATA_BACKEND=airtable`. Tabellschemat ar i princip identiskt mellan Postgres och Airtable.

---

## 1. Tabell: `settings`

*   **Syfte:** Lagrar den overgripande konfigurationen for det **aktuella aktiva eventet**. Denna rad uppdateras av **FGT Dashboard** nar en turneringsorganisator konfigurerar ett event.
*   **Viktiga Falt:**

| Faltnamn                  | Typ              | Beskrivning                                                              | Exempel                           |
| :------------------------ | :--------------- | :----------------------------------------------------------------------- | :-------------------------------- |
| `Name`                    | `text`           | Namnet pa installningsraden (ofta bara "Active Settings").               | `Active Settings`                 |
| `is_active`               | `boolean`        | **MASTE VARA TRUE** for att systemet ska kanna igen raden som den aktiva konfigurationen. | `true`                            |
| `active_event_slug`       | `text`           | Den unika "sluggen" fran Start.gg for den aktiva turneringen.           | `fgc-trollhattan-weekly-42`       |
| `event_date`              | `date`           | Startdatumet for turneringen (hamtas fran Start.gg).                      | `2025-12-17`                      |
| `default_game`            | `text[]`         | Lista over spel som ar valda av TO att visas i formularet. Fylls fran Start.gg events. | `["Street Fighter 6", "Tekken 8"]` |
| `events_json`             | `jsonb`          | JSON-data med all info om eventen fran Start.gg, inklusive `tournament_entrants` for no-show-berakning. | `{"tournament_entrants": 38, "events": [...]}` |
| `startgg_event_url`       | `text`           | Den ursprungliga URL:en till Start.gg-turneringen. Rensas vid reopen.   | `https://start.gg/tournament/...` |
| `tournament_name`         | `text`           | Namnet pa turneringen (fran Start.gg).                                  | `FGC Trollhattan Weekly #42`      |
| `timezone`                | `text`           | Tidzonen for turneringen (fran Start.gg).                               | `Europe/Stockholm`                |
| `swish_number`            | `text`           | Swish-nummer for betalningar (om anvant).                                | `1234567890`                      |
| `swish_expected_per_game` | `numeric`        | Forvantad Swish-betalning per spel (i SEK).                             | `50`                              |
| `require_payment`         | `boolean`        | Om `true`, maste `payment_valid` vara sann for att bli `Ready`.          | `true`                            |
| `require_membership`      | `boolean`        | Om `true`, maste `member` vara sann for att bli `Ready`.                 | `true`                            |
| `require_startgg`         | `boolean`        | Om `true`, maste `startgg` vara sann for att bli `Ready`.                | `true`                            |
| `collect_acquisition_source` | `boolean`     | Om `true`, visas fragan "Hur hittade du eventet?" i check-in-formularet. | `false`                        |

---

## 2. Tabell: `active_event_data`

*   **Syfte:** Lagrar **live-data** for varje deltagares incheckning for det aktiva eventet. Denna tabell uppdateras primärt av **Backend** (via orchestrate-flödet). n8n rapporterar resultat till backend, som i sin tur skriver till denna tabell.
*   **Viktiga Falt:**

| Faltnamn                      | Typ              | Beskrivning                                                              | Exempel                                    |
| :---------------------------- | :--------------- | :----------------------------------------------------------------------- | :----------------------------------------- |
| `id`                          | `serial`         | Primar nyckel (auto-increment).                                          | `42`                                       |
| `name`                        | `text`           | Deltagarens fullstandiga namn (tvattat).                                 | `Anna Andersson`                           |
| `tag`                         | `text`           | Deltagarens gamer-tag (tvattat). Anvands for matchning mot Start.gg.      | `CoolPlayer`                               |
| `email`                       | `text`           | Deltagarens e-postadress (hamtas fran Start.gg vid check-in/bulk recheck). | `anna@example.com`                         |
| `telephone`                   | `text`           | Deltagarens telefonnummer (tvattat, endast siffror).                     | `0701234567`                               |
| `UUID`                        | `text`           | En unik identifierare for denna incheckning.                              | `b7d4e1f8-c2a7-...`                         |
| `player_uuid`                 | `text`           | Canonical player UUID — kopplar ihop spelaren over flera event. Matchas vid check-in via `_find_player_uuid()`. | `a1b2c3d4-...`                             |
| `external_id`                 | `text`           | Extern ID, t.ex. fran Start.gg-registrering.                             | `startgg_reg_12345`                        |
| `event_slug`                  | `text`           | Start.gg-sluggen for eventet deltagaren checkar in till.                 | `fgc-trollhattan-weekly-42`                |
| `startgg_event_id`            | `text`           | ID for det specifika Start.gg-eventet (t.ex. for ett visst spel).         | `123456`                                   |
| `tournament_games_registered` | `text[]`         | Lista over spel som deltagaren ar registrerad for i turneringen.          | `["Street Fighter 6", "Tekken 8"]`         |
| `member`                      | `boolean`        | `true` om deltagaren ar verifierad medlem i foreningen (via eBas).       | `true`                                     |
| `startgg`                     | `boolean`        | `true` om deltagaren ar verifierad registrerad pa Start.gg for eventet.  | `true`                                     |
| `is_guest`                    | `boolean`        | `true` om spelaren **inte** hittades pa Start.gg. Satts automatiskt.      | `true`                                     |
| `added_via`                   | `text`           | Kallan till raden (`startgg_flow`, `manual_dashboard`, `api`, `reopen_restore`, `unknown`). | `startgg_flow` |
| `acquisition_source`          | `text`           | Hur spelaren hittade eventet (om insamling ar aktiv).                     | `discord`                                  |
| `payment_amount`              | `numeric`        | Belopp som deltagaren har betalat (fran Swish-matchning eller manuellt). | `100`                                      |
| `payment_expected`            | `numeric`        | Forväntat betalningsbelopp.                                              | `100`                                      |
| `payment_valid`               | `boolean`        | `true` om betalningen har verifierats och ar korrekt.                     | `true`                                     |
| `status`                      | `text`           | Overgripande status for deltagaren: `Ready`, `Pending`.                  | `Ready`                                    |
| `created`                     | `timestamptz`    | Tidstampel for nar incheckningsraden skapades.                           | `2025-12-17T11:30:00Z`                     |

---

## 3. Tabell: `players`

*   **Syfte:** Masterlista over alla spelare, oberoende av event. Anvands for canonical identity tracking och Insights-spelarleaderboard.
*   **Viktiga Falt:**

| Faltnamn              | Typ              | Beskrivning                                                    | Exempel                         |
| :-------------------- | :--------------- | :------------------------------------------------------------- | :------------------------------ |
| `uuid`                | `text`           | Canonical player UUID (PK). Samma UUID anvands i `active_event_data.player_uuid` och `event_archive.player_uuid`. | `a1b2c3d4-...`                  |
| `name`                | `text`           | Spelarens namn.                                                | `Anna Andersson`                |
| `tag`                 | `text`           | Spelarens gamer-tag.                                           | `CoolPlayer`                    |
| `email`               | `text`           | Spelarens email.                                               | `anna@example.com`              |
| `telephone`           | `text`           | Spelarens telefon.                                             | `0701234567`                    |
| `games_played`        | `text[]`         | Lista over spel spelaren deltagit i.                           | `["Street Fighter 6"]`          |
| `total_events`        | `integer`        | Antal event spelaren deltagit i.                               | `5`                             |
| `total_paid`          | `numeric`        | Totalt belopp betalat.                                         | `250`                           |
| `first_seen`          | `timestamptz`    | Nar spelaren forst registrerades.                              | `2025-12-17T11:30:00Z`          |
| `last_seen`           | `timestamptz`    | Senaste aktivitet.                                             | `2026-03-10T18:00:00Z`          |
| `first_event`         | `text`           | Forsta event-sluggen spelaren syntes i.                        | `fightbox-1`                    |
| `last_event`          | `text`           | Senaste event-sluggen spelaren syntes i.                       | `fightbox-7`                    |
| `events_list`         | `jsonb`          | Historiklista over event spelaren deltagit i.                  | `["fightbox-1", "fightbox-2"]` |
| `notes`               | `text`           | Fritext-anteckningar.                                          | `Sponsor: Razer`                |

---

## 4. Tabell: `event_archive` (event_history)

*   **Syfte:** Arkiverad check-in-data per event. Fylls nar TO arkiverar ett event. Anvands for Insights-tabben.
*   **Viktiga Falt:** Samma falt som `active_event_data` plus:

| Faltnamn              | Typ              | Beskrivning                                                    |
| :-------------------- | :--------------- | :------------------------------------------------------------- |
| `player_uuid`         | `text`           | Canonical player UUID — bevaras fran active_event_data vid arkivering och aterstalls vid reopen. |
| `added_via`           | `text`           | Ursprunglig check-in-kalla bevarad i arkivet.                 |
| `acquisition_source`  | `text`           | Sparad acquisition-kalla per check-in-rad (om tillganglig).   |
| `archived_at`         | `timestamptz`    | Tidstampel for nar raden arkiverades.                          |

---

## 5. Tabell: `event_stats`

*   **Syfte:** Aggregerad statistik per event. Beraknas vid arkivering. Anvands for Insights KPI-kort.
*   **Viktiga Falt:**

| Faltnamn                    | Typ              | Beskrivning                                                    | Exempel        |
| :-------------------------- | :--------------- | :------------------------------------------------------------- | :------------- |
| `event_slug`                | `text`           | Start.gg-slug for eventet (PK).                               | `fightbox-2`   |
| `total_participants`        | `integer`        | Totalt antal deltagare som checkade in.                        | `34`           |
| `startgg_registered_count`  | `integer`        | Antal registrerade pa Start.gg (fran `events_json.tournament_entrants`). | `38`           |
| `checked_in_count`          | `integer`        | Antal incheckade (= `total_participants`).                     | `34`           |
| `no_show_count`             | `integer`        | Registrerade minus incheckade: `max(registered - checked_in, 0)`. | `4`            |
| `no_show_rate`              | `numeric(5,2)`   | No-show i procent: `(no_show / registered * 100)`.            | `10.53`        |
| `total_revenue`             | `numeric`        | Total betalad summa.                                           | `1700`         |
| `event_date`                | `date`           | Eventets datum.                                                | `2026-03-08`   |
| `archived_at`               | `timestamptz`    | Nar statistiken skapades/uppdaterades.                         | `2026-03-09T...` |

---

## 6. Tabell: `audit_log`

*   **Syfte:** Loggar viktiga systemhandelser for sparbarhet och felsokning.
*   **Viktiga Falt:**

| Faltnamn    | Typ              | Beskrivning                                            |
| :---------- | :--------------- | :----------------------------------------------------- |
| `id`        | `serial`         | Primar nyckel.                                         |
| `action`    | `text`           | Typ av handelse (t.ex. `checkin`, `archive`, `reopen`). |
| `actor`     | `text`           | Vem som utforde handelsen (TO-namn eller `system`).    |
| `details`   | `jsonb`          | Extra data om handelsen.                               |
| `created_at`| `timestamptz`    | Tidstampel.                                            |

---

## 7. Namnkonventioner

### Faltnamn: `tag` (inte `gametag` eller `gamerTag`)

Systemet anvander **`tag`** som standardnamn for spelarens "gamer-tag". Detta ar konsekvent over hela kodbasen for att undvika forvirring. Namnet `tag` ar kort, universellt och fungerar i alla tekniska kontexter (Python, JS, etc.).

### Canonical Player UUID

`player_uuid` ar systemets satt att koppla ihop samma spelare over flera event, aven om de stavar sitt namn/tag olika. UUID:t matchas vid check-in via `_find_player_uuid()` (soker pa tag och email i `players`-tabellen) och lagras i bade `active_event_data` och `event_archive`.
