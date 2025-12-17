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
*   **Viktiga Fält:**

| Fältnamn                      | Typ              | Beskrivning                                                              | Exempel                                    |
| :---------------------------- | :--------------- | :----------------------------------------------------------------------- | :----------------------------------------- |
| `name`                        | `Single line text` | Deltagarens fullständiga namn (tvättat).                                 | `Anna Andersson`                           |
| `tag`                     | `Single line text` | Deltagarens gamer-tag (tvättat). Används för matchning mot Start.gg.      | `CoolPlayer`                               |
| `email`                       | `Email`          | Deltagarens e-postadress.                                                | `anna@example.com`                         |
| `telephone`                   | `Phone number`   | Deltagarens telefonnummer (tvättat, endast siffror).                     | `0701234567`                               |
| `personnummer`                | `Single line text` | Deltagarens personnummer (tvättat, endast siffror). Används för matchning mot Sverok. | `199001011234`                             |
| `UUID`                        | `Single line text` | En unik identifierare för denna incheckning (genereras av n8n).          | `b7d4e1f8-c2a7-...`                         |
| `external_id`                 | `Single line text` | Extern ID, t.ex. från Start.gg-registrering.                             | `startgg_reg_12345`                        |
| `event_slug`                  | `Single line text` | Start.gg-sluggen för eventet deltagaren checkar in till.                 | `fgc-trollhattan-weekly-42`                |
| `startgg_event_id`            | `Single line text` | ID för det specifika Start.gg-eventet (t.ex. för ett visst spel).         | `123456`                                   |
| `tournament_games_registered` | `Multi-select`   | Lista över spel som deltagaren är registrerad för i turneringen (från Start.gg). | `["Street Fighter 6", "Tekken 8"]`         |
| `member`                      | `Checkbox`       | `[x]` om deltagaren är verifierad medlem i föreningen.                   | `[x]`                                      |
| `startgg`                     | `Checkbox`       | `[x]` om deltagaren är verifierad registrerad på Start.gg för eventet.   | `[x]`                                      |
| `payment_amount`              | `Number`         | Belopp som deltagaren har betalat (från Swish-matchning eller manuellt). | `100`                                      |
| `payment_expected`            | `Number`         | Förväntat betalningsbelopp.                                              | `100`                                      |
| `payment_valid`               | `Checkbox`       | `[x]` om betalningen har verifierats och är korrekt.                     | `[x]`                                      |
| `status`                      | `Single select`  | Övergripande status för deltagaren: `Ready`, `Pending`, `Missing Membership`, `Missing Payment`, `Missing Start.gg`. | `Ready`                                    |
| `created`                     | `Created time`   | Tidstämpel för när incheckningsraden skapades.                           | `2025-12-17T11:30:00.000Z`                 |

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

Systemet använder **`tag`** som standardnamn för spelarens "gamer-tag" eller "nick". Detta beslut togs för att uppnå konsekvens över hela kodbasen:

| Plats | Fältnamn | Status |
|-------|----------|--------|
| Frontend (HTML/JS) | `tag` | ✅ Standard |
| Backend (Python) | `tag` | ✅ Standard |
| Airtable | `tag` | ✅ Standard |
| shared/airtable_api.py | `tag` | ✅ Standard |
| N8N workflows | `tag` | ✅ Standard |
| Start.gg API | `gamerTag` | ⚠️ Extern standard (kan ej ändras) |

**Varför `tag`?**
1. Kort och universellt - alla i FGC säger "what's your tag?"
2. Fungerar i alla språk och kontexter (Python, JS, SQL)
3. Ingen förvirring med Start.gg:s `gamerTag` - de jämförs bara, inte blandas

**Jämförelse med Start.gg:**
```javascript
// Vårt fält heter 'tag', deras heter 'gamerTag' - ingen konflikt
participant.gamerTag.toLowerCase() === ourData.tag.toLowerCase()
```

> **OBS:** Om du hittar `gametag` någonstans i koden är det en rest som bör bytas till `tag`.
