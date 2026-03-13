# Insights KPI Spec (v2)

## Purpose

Detta dokument laser KPI-definitioner for Insights sa att dashboard, rapporter och beslut anvander samma logik.

Prioritet for verksamheten: **Community -> Tillvaxt -> Kvalitet -> Intakter**.

## Scope and filters

- Insights beraknas pa vald kombination av:
  - period (rolling, custom, all time)
  - series-filter (optional)
  - event-filter (optional, multi)
- Tomt event-val betyder: "alla events inom vald period/filter".

## KPI Formula contract (Phase 2 lock)

Denna sektion laser "what over what" for KPI:er som anvands i korten.

| KPI | Numerator (what) | Denominator (over what) | Multi-event aggregation |
|-----|------------------|--------------------------|-------------------------|
| Participants | `sum(total_participants)` | - (count KPI) | Sum |
| Checked-in Slots | `sum(game entries)` | - (count KPI) | Sum |
| Ready Rate | `sum(ready_count)` | `sum(total_participants)` | Ratio of sums |
| Revenue | `sum(total_revenue)` | - (currency KPI) | Sum |
| New Players | `sum(new_players)` | - (count KPI) | Sum |
| Returning Players | `sum(returning_players)` | - (count KPI) | Sum |
| Retention Rate | `event_retention_rate` | `event_total_participants` | Weighted avg over events |
| Guest Share | `sum(guest_count)` | `sum(total_participants)` | Ratio of sums |
| Start.gg Account Rate | `sum(startgg_count - guest_count)` | `sum(total_participants)` | Ratio of sums |
| Member Rate | `sum(member_count)` | `sum(total_participants)` | Ratio of sums |
| Avg Games/Player | `total_slots` | `sum(total_participants)` | Ratio of sums |
| Multi-Game Players | `count(players with 2+ games)` | `sum(total_participants)` | Ratio of sums |
| No-Show Rate | `sum(startgg_registered_count - checked_in_count)` | `sum(startgg_registered_count)` | Ratio of sums |
| Coverage | `sum(checked_in_count)` | `sum(startgg_registered_count)` | Ratio of sums |
| Manual Share | `sum(manual_count)` | `sum(total_participants)` | Ratio of sums |

### Edge-case rules (locked)

- Om denominator = 0 -> visa `-` (unknown/not applicable), inte `0%`.
- Om numerator finns men denominator saknas -> visa `-` och logga datakvalitetsvarning.
- For count/currency KPI:er anvands `0` endast nar verkligt varde ar noll.
- For rates i multi-event scope anvands ratio-of-sums om inte annat anges explicit.
- Undantag: Retention Rate ar weighted average over events (weight = event participants).

---

## KPI Categories

KPI:er ar uppdelade i fem kategorier. Varje kategori svarar pa en nyckelfraga for TO.

| Category | Key Question |
|----------|-------------|
| **Core Event** | Hur stort var eventet? |
| **Community Health** | Vaxer scenen? |
| **Tournament Health** | Var turneringen bra? |
| **Operations** | Hur effektiv var logistiken? |
| **Game Ecosystem** | Hur ser spelmixen ut? |

---

## Category 1: Core Event

Snabb overblick — de forsta siffrorna en TO tittar pa.

### Entries (Participants)
- Definition: totalt antal deltagare i vald scope.
- Formel: `sum(total_participants)`.
- Submetric: Unique Attendees (`count(distinct player_uuid)` fran event_archive).
- Status: **LIVE**

### Slots (Bracket Entries)
- Definition: totalt antal turneringsdeltaganden (en spelare i 3 spel = 3 slots).
- Formel: `sum(unnest(tournament_games_registered))` fran event_archive.
- Varfor: Visar verklig turneringsvolym, inte bara headcount.
- Dashboard label: **Checked-in Slots**
- Status: **LIVE**

### Ready Rate (%)
- Definition: andel "Ready" i arkiverad snapshot.
- Formel: `sum(ready_count) / sum(total_participants) * 100`.
- Datakalla: `status_breakdown["Ready"]` i `event_stats`.
- Status: **LIVE**

### Revenue (kr)
- Definition: total intakt i vald scope.
- Formel: `sum(total_revenue)`.
- Status: **LIVE**

---

## Category 2: Community Health

Svarar pa "vaxer scenen eller stagnerar den?"

### New Players
- Definition: spelare som deltar for forsta gangen (aldrig setts i event_archive/players fore).
- Formel: `sum(new_players)` fran event_stats.
- Status: **LIVE**

### Returning Players
- Definition: spelare som deltagit i minst ett tidigare event.
- Formel: `sum(returning_players)` fran event_stats.
- Status: **LIVE**

### Retention Rate (%)
- Definition: andel aterkommande spelare i vald scope.
- Formel: viktad retention over events (INTE enkel ratio per period):
  - `weighted_avg(event_retention_rate, weight=event_total_participants)`.
  - Varje events `retention_rate` beraknas vid arkivering (returning_players / total_participants).
  - Multi-event scope viktas sa storre events har mer inflytande.
- Riktvarden:
  - `< 30%` — svag scen
  - `40-50%` — normal local
  - `50-70%` — stark scen
- Status: **LIVE**

### Core Players
- Definition: spelare med 3+ events senaste 6 manaderna (strikt — events INOM perioden).
- Formel:
  ```sql
  SELECT count(*) FROM (
    SELECT player_uuid
    FROM event_archive ea
    JOIN event_stats es ON ea.event_slug = es.event_slug
    WHERE es.archived_at >= now() - interval '6 months'
    GROUP BY player_uuid
    HAVING count(distinct ea.event_slug) >= 3
  ) core
  ```
- OBS: `players.total_events` ar totalt historiskt, inte inom tidsfonstret. Anvand event_archive.
- Varfor: Visar storleken pa den "riktiga" communityn — de som faktiskt stannar.
- Status: **LIVE**

### Player Lifetime
- Definition: genomsnittligt antal events per spelare.
- Formel: `avg(total_events)` fran `players`-tabellen.
- Varfor: Visar hur "klistrig" scenen ar — hog lifetime = folk kommer tillbaka.
- Status: **LIVE**

### Guest Share (%)
- Definition: andel guests i vald scope.
- Formel: `sum(guest_count) / sum(total_participants) * 100`.
- Status: **LIVE**

### Start.gg Account Rate (%)
- Definition: andel Start.gg-verifierade (exklusive guests) i vald scope.
- Formel: `sum(startgg_count - guest_count) / sum(total_participants) * 100`.
- OBS: Denominator ar `total_participants` (alla), namnare ar startgg-verifierade MINUS guests.
  Detta ar den logik dashboarden anvander — guests exkluderas fran namnaren for att de per
  definition inte har Start.gg-konto.
- Status: **LIVE**

### Member Rate (%)
- Definition: andel medlemmar i vald scope.
- Formel: `sum(member_count) / sum(total_participants) * 100`.
- Status: **LIVE**

---

## Category 3: Tournament Health

Svarar pa "var turneringen bra for spelarna?"

### Average Games per Player
- Definition: genomsnittligt antal spel per deltagare.
- Formel: `total_slots / total_participants`.
- Varfor: Hogt varde = folk far spela mycket = bra turnering.
- Status: **LIVE**

### Multi-Game Players (%)
- Definition: andel spelare som deltar i 2+ spel.
- Formel: `count(players with 2+ games) / total_participants * 100`.
- Datakalla: `tournament_games_registered` i event_archive.
- Riktvarden:
  - `< 10%` — separerade spelgrupper
  - `20-30%` — normal FGC
  - `40%+` — stark community-mix
- Status: **LIVE**

### No-Show Rate (%)
- Definition: andel registrerade pa Start.gg som inte checkade in.
- Formel: `(startgg_registered_count - checked_in_count) / startgg_registered_count * 100`.
- Datakalla: `startgg_registered_count`, `checked_in_count` i event_stats.
- Status: **LIVE**

### Coverage (%)
- Definition: checked-in vs registrerade.
- Formel: `checked_in_count / startgg_registered_count * 100`.
- Status: **LIVE** (visas i Events-tabellen, ej eget KPI-kort)

---

## Category 4: Operations

Hjalper TO:n under och efter events.

### Manual Share (%)
- Definition: andel check-ins som lades till manuellt via dashboard.
- Formel: `count(added_via='manual_dashboard') / total_participants * 100`.
- Datakalla: `event_archive.added_via` + `get_event_manual_add_stats()`.
- Varfor: Hog manual share = nagot i check-in-flodet funkar inte bra.
- Status: **LIVE**

---

## KPI Type classification (Phase 2 kickoff)

| KPI | Type |
|-----|------|
| Participants | Critical |
| Checked-in Slots | Informational |
| Ready Rate | Operational |
| Revenue | Informational |
| New Players | Critical |
| Returning Players | Critical |
| Retention Rate | Critical |
| Guest Share | Informational |
| Start.gg Account Rate | Informational |
| Member Rate | Informational |
| Average Games per Player | Critical |
| Multi-Game Players | Informational |
| No-Show Rate | Critical |
| Coverage | Operational |
| Manual Share | Operational |

### Check-in Speed
- Definition: antal check-ins per minut fran forsta till sista.
- Formel: `total_participants / (last_checkin_time - first_checkin_time)`.
- Krav: timestamps finns i `event_archive.archived_at` (approx).
- Status: **LIVE (Operations KPI card + Live Check-ins status chip)** — v1 driven by `checkin_opened_at` and current participants.

### Tournament Duration
- Definition: tid fran event start till slut.
- Krav: nya falt `event_started_at`, `event_ended_at` i settings/event_stats.
- Status: **LIVE (Operations KPI card + Live Check-ins elapsed/duration chip)** when timestamps are set.

---

## Category 5: Game Ecosystem

FGC-specifika metrics — "hur ser spelmixen ut?"

### Most Popular Game
- Definition: spelet med flest entries i vald scope.
- Datakalla: `games_breakdown` i event_stats / `most_popular_game`.
- Status: **LIVE** (visas i Games-tabellen)

### Players per Game
- Definition: antal spelare per spel i vald scope.
- Datakalla: `games_breakdown` i event_stats.
- Status: **LIVE** (visas i Games-tabellen)

### Game Popularity Trend
- Definition: spelare per spel over tid (per event).
- Formel: `games_breakdown` per event, plottat som tidsserie.
- Varfor: Visar om t.ex. Tekken vaxer, SF6 minskar, Smash stabilt.
- Status: **LIVE (v1 trend graph in Games view)**

### Game Crossover
- Definition: vilka spelkombinationer spelare valjer (t.ex. Tekken+SF6).
- Formel: `count(distinct player_uuid)` per unik game-kombination fran event_archive.
- Varfor: Visar community-struktur — ar det separerade grupper eller overlapp?
- Status: **LIVE (v1 top-pairs table in Games view)**

### Game Trend Shift (Biggest Mover)
- Definition: spelet med storst forandring i genomsnittliga entries/event mellan senare och tidigare halvan av vald period.
- Formel (v1): `avg_entries_second_half - avg_entries_first_half` per spel; visa storsta absoluta diff.
- Varfor: snabb signal om vilket spel som ror sig mest upp/ned i scope.
- Status: **LIVE (v1 text summary in Games view)**

---

## Advanced Metrics (Phase 6)

Inte nodvandiga nu, men varde nar mer historik finns.

### Player Funnel
- Definition: New -> Returning -> Core pipeline.
- Visar community-mognad over tid.
- Status: **LIVE (v2 summary in Players view)** — New -> Returning -> Core (6m) -> Churned (8m, global).
- Scope-regel: visas i period/series-scope; single-event visar notis om att funnel ar multi-event.

### Player Churn
- Definition: spelare som inte atervant efter senaste event.
- Formel: `count(players where last_seen < threshold AND total_events > 0)`.
- Status: **LIVE (Community KPI card)** — v1 uses 3-month inactivity window in selected scope.

### Community Growth Rate
- Definition: forandring i spelarantal mellan perioder.
- Formel: `(current_players - previous_players) / previous_players * 100`.
- Status: **LIVE (Community KPI card)** — v1 compares unique attendees vs previous same-length period.

### Player Acquisition Source
- Definition: hur spelare hittade eventet (friend, Discord, Start.gg, social media, venue).
- Krav: ny check-in-fraga i formularet.
- Status: **LIVE (v1)** — samlas in via check-in nar togglen `collect_acquisition_source` ar pa.
- Insights visning: `Acq known` + separat `missing` andel for legacy/saknade rader.

### Run Status per Game
- Definition: om ett spel faktiskt spelades, inte bara var registrerat.
- Datakallor: `startgg_snapshot.events[].numEntrants`, `setCount`, `gamesPlayed`.
- Regler (v1):
  - `Played` = `gamesPlayed > 0`
  - `No games played` = `numEntrants > 0` och `setCount > 0` men `gamesPlayed = 0`
  - `Registered only` = `numEntrants > 0` och inga spelade games
  - `No entrants` = `numEntrants = 0`
- Status: **LIVE (Games table)**

### Sets Played / Games Played
- Definition:
  - `Sets played` = antal sets/matches per spel
  - `Games played` = summerad game score over sets (BO3/BO5-volym)
- Status: **LIVE (Games table + totals in games header)**

---

## New member definition

- "Ny medlem" = spelare som inte var medlem vid check-in-start, men blev medlem via:
  1. medlemskrav i check-in-flodet, eller
  2. frivillig medlemsregistrering pa Ready-sidan (guest-flode).

## Delta definition (vs previous period)

- Delta visas mot omedelbart foregaende period med samma langd.
- Galler for perioder med datumintervall (day/week/month/quarter/year/custom).
- Delta visas inte for:
  - all time,
  - explicit event-val (specifika event slugs).

Format:
- count KPI: `up/down +N vs prev`
- rate KPI: `up/down +X.X pp vs prev`
- revenue KPI: `up/down +X kr vs prev`

## Benchmark ranges (Phase 2 lock)

Rangena ar "coach notes" for trendtolkning, inte hard alarms.

| KPI | Strong | Healthy | Watch |
|-----|--------|---------|-------|
| Ready Rate | >= 95% | 90-94% | < 90% |
| Retention Rate | >= 50% | 40-49% | < 40% |
| No-Show Rate | < 8% | 8-15% | > 15% |
| Manual Share | < 10% | 10-20% | > 20% |
| Start.gg Account Rate | >= 80% | 60-79% | < 60% |
| Guest Share | 5-20% | 0-4% or 21-30% | > 30% |
| Member Rate | >= 50% | 30-49% | < 30% |
| Multi-Game Players | >= 30% | 15-29% | < 15% |
| Coverage | >= 90% | 75-89% | < 75% |

## Heads-up language

- Insights ska anvanda "Heads-up" eller "Coach notes" i stallet for alarmton.
- v1 anvander trend (delta) plus kontext.
- Fasta troskelvarden kan laggas i senare version nar mer historik finns.

## Data quality rules

- Saknad data visas som `-` (unknown), inte `0`.
- `0` anvands endast nar matvardet faktiskt ar noll.
- Archive-flodet loggar soft integrity warnings vid mismatch (funnel/no-show/payment/game-consistency).
- Advanced-tools:
  - `Recompute Selected Event` (owner/dev)
  - `Scan Archived Events` (TO-visible) for att hitta events med varningar.
