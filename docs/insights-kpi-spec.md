# Insights KPI Spec (v1)

## Purpose
Detta dokument laser KPI-definitioner for Insights sa att dashboard, rapporter och beslut anvander samma logik.

Prioritet for verksamheten: **Community -> Tillvaxt -> Kvalitet -> Intakter**.

## Scope and filters
- Insights beraknas pa vald kombination av:
  - period (rolling, custom, all time)
  - series-filter (optional)
  - event-filter (optional, multi)
- Tomt event-val betyder: "alla events inom vald period/filter".

## KPI definitions

### Participants
- Definition: totalt antal deltagare i vald scope.
- Formel: `sum(total_participants)`.

### Ready Rate (%)
- Definition: andel "Ready" i arkiverad snapshot.
- Formel: `sum(ready_count) / sum(total_participants) * 100`.
- Datakalla: `status_breakdown["Ready"]` i `event_stats`.

### Member Rate (%)
- Definition: andel medlemmar i vald scope.
- Formel: `sum(member_count) / sum(total_participants) * 100`.

### Guest Share (%)
- Definition: andel guests i vald scope.
- Formel: `sum(guest_count) / sum(total_participants) * 100`.

### Start.gg Rate (%)
- Definition: andel Start.gg-verifierade i vald scope.
- Formel: `sum(startgg_count) / sum(total_participants) * 100`.

### Retention (%)
- Definition: andel aterkommande spelare i vald scope.
- Formel (v1): viktad retention fran eventniva:
  - `weighted_avg(event_retention_rate, weight=event_total_participants)`.

### Revenue (kr)
- Definition: total intakt i vald scope.
- Formel: `sum(total_revenue)`.

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

## Heads-up language
- Insights ska anvanda "Heads-up" eller "Coach notes" i stallet for alarmton.
- v1 anvander trend (delta) plus kontext.
- Fasta troskelvarden kan laggas i senare version nar mer historik finns.

## Data quality rules
- Saknad data visas som `-` (unknown), inte `0`.
- `0` anvands endast nar matvardet faktiskt ar noll.

## Next iterations
1. KPI-deltas med tydlig trend i UI.
2. Export (CSV/PDF/JSON) per Insights-vy.
3. No-show metric via Start.gg registrerade vs check-ins.
4. Explicit `series_key` i datamodell for 100 procent seriegruppering.
