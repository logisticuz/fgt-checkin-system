# Insights Roadmap

> Fasplan for att bygga ut Insights-dashboarden fran nuvarande 7 KPI-kort till en fullstandig
> FGC community analytics-plattform. Varje fas bygger pa foregaende — ghor inte fas N+1 forstfoerre fas N ar klar.

## Status overview

| Phase | Name | Status | KPIs added |
|-------|------|--------|------------|
| 0 | Baseline (done) | DONE | 7 KPI-kort + stodtabeller |
| 1 | Category Layout + Quick Wins | DONE | +3 nya KPIs + 4 promoted, kategori-UI |
| 2 | KPI Contracts | DONE | spec + edge cases |
| 3 | Community Health v2 | DONE | +2 KPIs, funnel |
| 4 | Game Ecosystem v2 | DONE | +2 KPIs, trendgrafer |
| 5 | Operations Live | DONE | +2 KPIs, ny datainsamling |
| 6 | Advanced Analytics | DONE | +4 KPIs, ny check-in-fraga |

---

## Phase 0: Baseline (DONE)

**Status:** Klar. Det har ar vad som finns idag.

### KPI-kort (7 st)
1. Entries (Participants) — med "X unique attendees" submetric
2. Ready Rate (%)
3. Member Rate (%)
4. Guest Share (%)
5. Start.gg Account Rate (%)
6. Retention (%)
7. Total Revenue (kr)

### Stodtabeller
- Top Attendees (leaderboard, grupperat pa player_uuid)
- Most Played Games (rankat, med procentandel)
- Events (per-event breakdown med no-show, manual %, revenue)
- Earnings (per-event intakter)
- Duplicates (merge-verktyg)

### Data already available (not yet shown as KPI cards)
- `new_players`, `returning_players` — finns i event_stats
- `no_show_rate`, `no_show_count` — finns i event_stats
- `startgg_registered_count`, `checked_in_count` — finns i event_stats
- `games_breakdown` — finns i event_stats (JSONB)
- `added_via` breakdown — finns i event_archive

---

## Phase 1: Category Layout + Quick Wins

**Goal:** Organisera KPI:er i kategorier och lagg till 7 KPI-kort totalt: 3 helt nya (Slots, Avg Games, Multi-Game) + 4 promoted fran tabelldata till egna kort (New Players, Returning Players, No-Show Rate, Manual Share).

**Estimated effort:** 1-2 sessioner

### 1.1 UI: Kategoriindelning

Organisera KPI-korten i visuella sektioner med rubrik:

```
[Core Event]          Entries | Slots | Ready Rate | Revenue
[Community Health]    New Players | Returning | Retention | Guest Share | Start.gg Rate | Member Rate
[Tournament Health]   Avg Games/Player | Multi-Game % | No-Show Rate
[Operations]          Manual Share
```

Implementation: Lagg till sektionsrubriker i `layout.py`, gruppera korten.

### 1.2 Nya KPI-kort (3 helt nya)

**Slots (Bracket Entries)**
- Formel: `sum(length(tournament_games_registered))` per event fran event_archive
- ELLER: `sum(values from games_breakdown)` fran event_stats
- Delta: count format
- Kategori: Core Event

**Average Games per Player**
- Formel: `slots / participants`
- Delta: count format med 1 decimal
- Kategori: Tournament Health

**Multi-Game Players (%)**
- Formel: `count(rows with 2+ games in tournament_games_registered) / total_participants * 100`
- Datakalla: event_archive
- Delta: pp format
- Kategori: Tournament Health

### 1.3 Promote befintliga metrics till KPI-kort (3 promoted)

Dessa metrics FINNS redan i event_stats men visas bara i tabellvyer. Promota till egna KPI-kort:

- **New Players** — redan i event_stats (`new_players`), exponera som kort i Community Health
- **Returning Players** — redan i event_stats (`returning_players`), exponera som kort i Community Health
- **No-Show Rate** — redan i event_stats (`no_show_rate`), exponera som kort i Tournament Health

### 1.4 Manual Share till Operations-kategori

- **Manual Share (%)** — redan beraknad via `get_event_manual_add_stats()`, visas i Events-tabell
- Promota till eget KPI-kort under Operations-sektionen
- Formel: `count(added_via='manual_dashboard') / total_participants * 100`
- Delta: pp format
- Varfor som eget kort: hog manual share signalerar att check-in-flodet har problem

### Phase 1 completion notes

- Kategori-UI ar implementerad med kompakt kategori-switcher (Core, Community, Tournament, Operations, All).
- KPI-kort for Slots, Avg Games/Player, Multi-Game %, New Players, Returning Players, No-Show Rate och Manual Share ar live.
- Delta-berakning ar inkopplad for nya KPI-kort med samma previous-period-logik som ovriga kort.
- Help-text finns for samtliga nya KPI-kort.

### Definition of Done
- [x] KPI-kort visas i kategorisektioner med rubriker
- [x] Slots, Avg Games/Player, Multi-Game % fungerar med delta
- [x] New Players, Returning Players, No-Show Rate har egna kort
- [x] Manual Share har eget kort under Operations
- [x] Totalt ~14 KPI-kort fordelat pa 4 kategorier (Core, Community, Tournament, Operations)

---

## Phase 2: KPI Contracts

**Goal:** Skriv exakta definitioner for VARJE KPI sa att alla (backend, dashboard, teamet) tolkar dom lika.

**Estimated effort:** 1 session (spec-arbete, inte kod)

### Phase 2 completion notes

- `insights-kpi-spec.md` ar synkad med nuvarande live-lage for Phase 1 KPI:er.
- KPI formula contract ar last (numerator/denominator + aggregation).
- Edge-case-regler ar explicit satta (denominator=0 -> `-`, ratio-of-sums default).
- KPI type classification ar satt (Critical / Operational / Informational).
- Benchmark-ranges ar dokumenterade for rate-KPI:er.

### 2.1 Per-KPI spec

For varje KPI, dokumentera:
- **Exact formula** med SQL-uttryck
- **Denominator edge cases** (vad hander om namnaren ar 0?)
- **Multi-event aggregation** (sum? weighted avg? latest?)
- **KPI type**: Critical / Operational / Informational
- **Benchmark ranges** (nar ar vardet "bra" vs "daligt"?)

**Known ambiguities to resolve in this phase:**
- **Start.gg Account Rate**: dashboarden anvander `(startgg_count - guest_count) / total_participants`
  (non-guest logik). Las detta explicit — denominator ar total_participants, namnare ar
  startgg-verifierade EXKLUSIVE guests.
- **Retention**: nuvarande implementation anvander viktad retention over events
  (`weighted_avg(event_retention_rate, weight=event_total_participants)`), INTE enkel ratio per
  period. Las att detta ar den officiella definitionen.

### 2.2 KPI type classification

| Type | Meaning | Example |
|------|---------|---------|
| Critical | Direkt TO-beslut | Retention, No-Show |
| Operational | Hjalper under event | Ready Rate, Manual Share |
| Informational | Kontext/insikt | Start.gg Rate, Member Rate |

### Definition of Done
- [x] `insights-kpi-spec.md` uppdaterad med exakta formler + edge cases for alla KPIs
- [x] Benchmark-ranges dokumenterade for rate-KPIs
- [x] KPI-typer (Critical/Operational/Informational) satta

---

## Phase 3: Community Health v2

**Goal:** Lagg till djupare community-metrics som kraver player-tabelldata.

**Estimated effort:** 2-3 sessioner

### 3.1 Nya KPI-kort (2 st)

**Core Players**
- Definition: spelare med 3+ events senaste 6 manaderna (strikt — events INOM perioden, inte totalt)
- Formel:
  ```sql
  SELECT count(distinct player_uuid)
  FROM event_archive ea
  JOIN event_stats es ON ea.event_slug = es.event_slug
  WHERE es.archived_at >= now() - interval '6 months'
  GROUP BY player_uuid
  HAVING count(distinct ea.event_slug) >= 3
  ```
- OBS: `players.total_events` racker INTE — den visar totalt historiskt, inte inom tidsfonstret.
  Event-baserad rakning via `event_archive` ar korrekt.
- Kategori: Community Health

**Player Lifetime**
- Definition: genomsnittligt antal events per spelare
- Formel: `SELECT avg(total_events) FROM players WHERE total_events > 0`
- Kategori: Community Health

### 3.2 Player Funnel (visualisering)

Visa en enkel funnel/bar:
```
New Players (first event) ——> Returning (2+ events) ——> Core (3+ events/6m)
```
- Datakalla: `players`-tabellen, grupperat pa total_events
- Visualisering: horisontell bar chart eller sankey-liknande

### 3.3 Data validation

- Verifiera att `players.total_events` och `events_list` uppdateras korrekt vid arkivering
- Verifiera att `player_uuid`-koppling i event_archive ar konsekvent

### Definition of Done
- [x] Core Players och Player Lifetime visas som KPI-kort
- [x] Player Funnel-visualisering fungerar
- [x] players-tabellens total_events/last_seen veriferad mot event_archive

---

## Phase 4: Game Ecosystem v2

**Goal:** Visa hur spelmixen forandras over tid och hur spelare ror sig mellan spel.

**Estimated effort:** 2-3 sessioner

### 4.1 Game Popularity Trend (visualisering)

- Trendgraf: X-axel = events (kronologiskt), Y-axel = antal spelare per spel
- En linje per spel (fargkodade)
- Datakalla: `games_breakdown` per event i event_stats
- Gor det mojligt att se: "Tekken vaxer, SF6 minskar"

### 4.2 Game Crossover (ny metric + visualisering)

- Berakna: for varje unik kombination av 2 spel, hur manga spelare spelar bada?
- Formel:
  ```sql
  SELECT game_a, game_b, count(distinct player_uuid)
  FROM (crossjoin of player games)
  GROUP BY game_a, game_b
  ```
- Visualisering: matris/heatmap eller enkel tabell
- Kategori: Game Ecosystem

### 4.3 Game Trend Shift

- "Biggest mover" — spelet med storsta okningen/minskningen mellan tva perioder
- Visas som en liten notis under Games-sektionen

### Definition of Done
- [x] Game Popularity Trend-graf fungerar med befintlig data
- [x] Game Crossover-tabell/matris visas
- [x] Trender beraknas korrekt over multiple events

---

## Phase 5: Operations Live

**Goal:** Lagg till metrics som hjalper TO:n UNDER eventet, inte bara efterat.

**Estimated effort:** 2 sessioner + schema-andring

### 5.1 Schema changes

Lagg till i settings eller event_stats:
- `checkin_opened_at` (timestamp) — nar check-in oppnades
- `event_started_at` (timestamp) — nar turneringen startade
- `event_ended_at` (timestamp) — nar turneringen slutade

### 5.2 Nya KPI:er

**Check-in Speed**
- Formel: `total_participants / (last_checkin - checkin_opened_at)` i minutes
- Krav: `checkin_opened_at` maste satta

**Tournament Duration**
- Formel: `event_ended_at - event_started_at`
- Sweet spot for locals: 3-4 timmar

### 5.3 Dashboard UI for TO under event

- Visa live Check-in Speed pa Manage-tabben (inte bara Insights)
- Visa countdown/elapsed time

### Definition of Done
- [x] Schema uppdaterat med timestamp-falt
- [x] TO kan satta `checkin_opened_at` via dashboard
- [x] Check-in Speed och Tournament Duration visas i Insights
- [x] Live Check-in Speed + elapsed/duration visas i Live Check-ins (TO-view)

---

## Phase 6: Advanced Analytics

**Goal:** Djupare community-analytics for strategiska beslut. Kraver mer data och historik.

**Estimated effort:** 3+ sessioner

### 6.1 Player Churn

- Definition: spelare som INTE atervant efter sitt senaste event
- Formel: `count(players where last_seen < now() - interval '3 months' AND total_events > 0)`
- Krav: tillracklig eventkadens (minst 6+ events i historik)

### 6.2 Community Growth Rate

- Formel: `(players_this_period - players_prev_period) / players_prev_period * 100`
- Visas som trend over tid

### 6.3 Player Acquisition Source

- Krav: ny fraga i check-in-formularet: "Hur hittade du eventet?"
  - Alternativ: friend, Discord, Start.gg, social media, venue, other
- Sparas i event_archive som `acquisition_source`
- Visualisering: pie chart / bar chart per kalla

### 6.4 Player Funnel v2

- Full funnel: New -> Returning -> Core -> Churned
- Visar community lifecycle over tid

### 6.x Implementerat under Phase 6 (current state)

- Acquisition Source ar implementerad end-to-end:
  - toggle i Settings (collect_acquisition_source)
  - fraga i check-in-form
  - lagring i active/archive (`acquisition_source`)
  - Insights-sammanfattning med `known` vs `missing/legacy`
- Game run-signal ar utbyggd:
  - `Registered`, `Sets played`, `Games played`, `Run status`
  - status prioriterar faktiska `gamesPlayed` for att skilja "registered men ej spelat"
- Scope-aware KPI-visibility:
  - irrelevanta KPI-kort dolds/markeras beroende pa single-event vs multi-event scope
- Data quality guardrails:
  - soft integrity warnings vid archive
  - `Recompute Selected Event` (owner/dev)
  - `Scan Archived Events` (TO-visibility i Advanced)

### Definition of Done
- [x] Churn-metric beraknas och visas
- [x] Growth Rate-trend fungerar
- [x] Acquisition Source-fraga tillagd i check-in-formularet
- [x] Full player funnel visualiserad

---

## Priority rules

1. **Bygg bara KPI:er som leder till beslut.** Om en KPI inte andrar TO-beteende → lagg i senare fas.
2. **Data forst, UI sen.** Saker stall att datan beraknas ratt innan du bygger kort/grafer.
3. **Max 12-15 KPI-kort synliga.** For manga = brus. Anvand tabs/sektioner for att sprida ut.
4. **Testa mot riktig data.** Verifiera varje ny KPI mot befintliga arkiverade events innan release.

---

## Dashboard layout target (after Phase 1)

```
┌─── Core Event ────────────────────────────────────────┐
│ Entries (48)  │ Slots (127)  │ Ready Rate │ Revenue   │
│ 48 unique     │              │    92%     │ 3,200 kr  │
└───────────────────────────────────────────────────────┘

┌─── Community Health ──────────────────────────────────┐
│ New (12) │ Returning (36) │ Retention │ Guest │ Sgg % │
│          │                │   75%     │  8%   │  89%  │
└───────────────────────────────────────────────────────┘

┌─── Tournament Health ─────────────────────────────────┐
│ Avg Games/Player │ Multi-Game % │ No-Show Rate        │
│      2.6         │    35%       │     12%             │
└───────────────────────────────────────────────────────┘

┌─── Operations ────────────────────────────────────────┐
│ Manual Share                                          │
│    15%                                                │
└───────────────────────────────────────────────────────┘

[Top Attendees] [Most Played Games] [Events] [Earnings]
```

---

## Changelog

- 2026-03-12: v1 created. Based on ChatGPT/Codex KPI category discussion + current system state.
- 2026-03-12: v1.1 patched. Applied 5 Codex review corrections: accurate KPI count (+3 new +3 promoted), Core Players event-based query, Start.gg Rate denominator note, Retention weighted-avg lock, Manual Share as Operations KPI-kort.
- 2026-03-12: v1.2 updated. Phase 1 marked DONE after implementation and UX polish; Phase 2 moved to IN PROGRESS.
- 2026-03-12: v1.3 updated. Phase 2 marked DONE after KPI contract lock (formulas, edge cases, benchmark ranges, KPI types).
- 2026-03-12: v1.4 updated. Phase 3 started with Core Players + Player Lifetime KPI cards (funnel/validation pending).
- 2026-03-12: v1.5 updated. Added Player Funnel v1 in Players view; Phase 3 validation item remains.
- 2026-03-12: v1.6 updated. Completed Phase 3 validation against event_archive; fixed players last_seen/last_event drift and marked Phase 3 DONE.
- 2026-03-12: v1.7 updated. Started Phase 4 with Game Trend graph + Game Crossover table in Games view.
- 2026-03-12: v1.8 updated. Phase 4 marked DONE after trend/crossover refinements and biggest-mover normalization.
- 2026-03-12: v1.9 updated. Started Phase 5 with live operations timing fields in Settings and live check-in speed/tournament duration note in Insights.
- 2026-03-12: v1.10 updated. Promoted Check-in Speed and Tournament Duration to Operations KPI cards and added live timing quick-actions in Live Check-ins.
- 2026-03-12: v1.11 updated. Completed Phase 5 with live status chips (check-in speed + elapsed/duration) in Live Check-ins and marked phase DONE.
- 2026-03-12: v1.12 updated. Started Phase 6 with Growth Rate and Churn Rate KPI cards in Community Health.
- 2026-03-13: v1.13 updated. Phase 6 extended with Acquisition Source end-to-end (settings toggle + check-in + archive + insights summary), game run-status (registered/sets/games played), scope-aware KPI visibility, and integrity tooling (recompute + scan + archive warnings).
- 2026-03-13: v1.14 updated. Player Funnel v2 expanded to New -> Returning -> Core -> Churned (8m global churn signal), with single-event scope guardrails.
