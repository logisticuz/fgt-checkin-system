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
| Returning visits | `sum(returning_players)` | - (one UUID per event) | Sum |
| Familiar faces (`retention_rate`) | returning visits | unique participant visits | Ratio of sums |
| Came back from previous (same series) | shared UUIDs with previous event in same series | that previous event UUID cohort | No baseline for first/collab |
| Guest Share | `sum(guest_count)` | `sum(total_participants)` | Ratio of sums |
| Start.gg Account Rate | `sum(startgg_count - guest_count)` | `sum(total_participants)` | Ratio of sums |
| Member Rate | members at requirement-enabled events | participants at those events | Unknown if any historical requirement is unknown |
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
- First attendance uses event date, then the earliest recorded `archived_at`, then slug.
- One UUID is new at most once across the full archive. Selecting a period does not reset history.
- Unknown or conflicting event dates are excluded from new/returning and flagged.
- KPI: sum of per-event first arrivals, equivalent to unique first arrivals in scope.

### Returning visits
- One UUID counts once for each selected event after its first dated archived event.
- The KPI sums returning **visits**. The funnel counts **unique people** with a returning visit.
- A person may be new at one selected event and returning at another; funnel cohorts overlap.
- Attendance is counted once per `(event_slug, player_uuid)`. Duplicate check-ins remain archive facts and are flagged as counted once; all recorded payments are summed.
- Unlinked rows remain separate provisional attendances and are flagged; no identity merge or attendance is inferred.

### Familiar faces (%) and same-series return
- `retention_rate` keeps its historical meaning: returning visits / unique participant visits.
- Display it as **Familiar faces %**, weighted by current event participants across a selected scope.
- New remains GLOBAL first attendance, even when a series filter is active; never silently "new to series".
- **Came back from previous (same series)** is separate: the prior dated event in the same series supplies the UUID cohort; count the share attending again.
- Series are explicit slug prefixes `fightbox*` and `fight-night*`. Other collaborations have no series baseline.
- First-in-series and unknown dates show `–` for same-series return. First global event has 0% familiar faces.
- Date/archived_at/slug ties use the same order for global first attendance and previous-in-series selection.

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
- Future archives snapshot `require_payment`, `require_membership`, and `require_startgg` in `event_stats.requirements_snapshot` (migration 010).
- Re-archiving preserves the first snapshot. Today's settings never establish a past event's requirements.
- `require_membership=true`: actual member count / attendance count, including a real 0%.
- `false`: `– (not required)`. Missing historical snapshot: `– (unknown)`.
- `--member-not-recorded slug1,slug2` explicitly sets `member_data_recorded=false`: display `– (not recorded)` instead of a misleading 0%.
- The integrity scan always flags a membership-required event with zero recorded members, including an explicit data gap.
- Aggregate only requirement-enabled events; any unknown requirement or explicit missing membership data makes the aggregate unavailable.
- Verified free-event input establishes only `require_payment=false`, not membership or Start.gg policy.
- Backfill membership policy only with explicit `--required-membership slug1,slug2` or `--no-membership slug3`; conflicting assertions are rejected. Other requirements remain unchanged/unknown.

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
- Definition: unique first arrivals and unique returning visitors, using the canonical event-date rule above; cohorts can overlap.
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

## Archive repair (T-015)

`python scripts/recompute_stats.py` defaults to SELECT-only REPEATABLE READ / READ ONLY,
without initializing the application pool or running startup DDL. `--json` exports only event
counts, dates, money, and grouped deviation counts. `--player-details` adds UUID-level derived
totals/first-seen diffs for authorized review; contacts are never printed.

`--free-events slug1,slug2` is an explicit verified assertion. It resets expected payment and
records only the free requirement; existing nonzero payment amounts remain unchanged.
`--event-price slug=25` asserts a per-person price; `slug=25/game` uses only recorded nonempty
game counts, deduplicated through T065's exact canonical alias map when installed. These inputs change ONLY `payment_expected`; NULL, zero and nonzero recorded
`payment_amount` values are always preserved, including when `payment_valid=true`. A paid flag
with a missing/zero amount is a human deviation: "paid, amount not recorded". Underpayments and
unknown game counts are flagged. Neither payment amounts nor paid flags are inferred from a tariff.
Verified price inputs are stored as `requirements_snapshot.event_price={amount,basis}` (`game` or `person`)
so reopening can restore the event's actual tariff. A verified free assertion clears the inactive tariff.
No price is assumed from current settings or old expected amounts.
`--compare-price slug=25` is dry-run only and shows both bases without choosing one.

`--event` restricts event-stat repair and explicit inputs; player aggregates still use the whole
archive. `players.total_events` counts distinct event visits, `total_paid` sums every recorded payment amount, and
first/last seen use known event dates at UTC midnight. Duplicate facts remain flagged for a human.
Duplicate paid check-ins are not given automatic expected-payment edits; their single-attendance obligation needs review.
No-show is unknown when only game slots (not a unique registration count) were recorded.
Unknown no-show points remain gaps in the trend and show `n/a` in Ops, with no Low/OK rating.
All non-game numerators (Ready, Start.gg, guests and members) use the same unique visits as participants.

Apply migration 010 with the migration runner before a separately authorized `--apply`. The
repair and audit commit atomically; no provider is contacted. Apply refuses any active event/check-in rows or busy archive/activation; table and advisory locks protect that check.
The audit stores every changed field before/after with its archive row id, event slug or player UUID, including whether a new stats row existed. Profile metadata timestamps are not changed by derived repairs.
Before and after apply, reconcile ALL profile payment totals against archived payments and event revenue. Unassigned money or an off-scope revenue mismatch blocks apply. Counts-only output explains profiles without global first attendance (zero archive rows or only undated visits). Read-only dry-run supports a
pre-010 database. PROD apply is forbidden until after 2026-11-14 and the user's explicit OK.


### Games attendance population (C413)

Games use the same `(event_slug, player_uuid)` attendance units as participants. Each unit
contributes its DISTINCT canonical games, unioned across that attendee's archive rows using
the T065 alias map. Per-game counts, total slots, multi-game counts and derived player game
histories count a game once per attendance. Across events these are player-event visits.
Rows without a UUID each remain their own unit; names/tags never infer identity.
A recorded games list on any row makes that attendance's game metadata available; a NULL
duplicate row does not exclude an attendee whose recorded games contribute to the numerator.
No archive fact or payment is deleted/rewritten by this grouping. Crossover's explicitly
labelled "Shared players" remains a separate distinct-person overlap measure.

### Event date ranges (C417/C418)

Date ranges select whole events using `event_stats.event_date` first. If it is absent,
use the single distinct non-NULL archived date for that event; zero or conflicting dates
remain unknown. Every row from a selected event participates, even when a row's date is
NULL or disagrees with the event date. The canonical context uses the identical rule.
This applies to Games/multi-game, manual/added-via/acquisition sources, crossover,
unique/top participants, churn scope, community scope/core rolling windows and funnel/core.
Existing rolling-window lengths, distinct-person and attendance definitions stay intact.
All-time includes unknown-date events; bounded ranges exclude them and Insights explicitly
shows the count of events without a date. Unknown dates never become today's date.
