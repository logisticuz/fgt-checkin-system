# Arbetsorder: Normalisera spelnamn i game_counts

Senast uppdaterad: 2026-03-24

## Problem

`game_counts` i `players`-tabellen trackar spelnamn baserat pa **eventnamn**
fran Start.gg, inte det faktiska spelet. Arrangorer dopar events olika:

```
"SMASH SINGLES": 2
"SUPER SMASH BROS ULTIMATE - SINGLES": 5
```

Dessa ar samma spel men raknas separat. Resultatet ar att `favorite_game`
kan bli fel — en spelare som deltagit i 7 Smash-events kan visa SF6 som favorit
for att Smash ar uppdelat pa tva namn.

## Rotkorsak

GraphQL-queryn i n8n-flodet "FGC THN - Start.gg Check" hamtar bara:

```graphql
events { id name }
```

`name` ar eventnamnet (arrangors-satt, varierar). Start.gg har aven ett
`videogame`-falt pa varje event som ar **standardiserat av Start.gg** och
alltid samma oavsett vad arrangoren dopar eventet.

## Losning

### 1. Uppdatera GraphQL-queryn i n8n

I flodet "FGC THN - Start.gg Check", andra queryn fran:

```graphql
events { id name }
```

till:

```graphql
events { id name videogame { name } }
```

### 2. Uppdatera Parse Response-steget i n8n

I samma flode, andra parse-logiken sa att `tournament_games_registered`
anvander `videogame.name` istallet for `event.name`:

```javascript
// Tidigare:
const events = (player.events || []).map(e => e.name);

// Nytt:
const events = (player.events || []).map(e =>
    (e.videogame && e.videogame.name) ? e.videogame.name : e.name
);
```

Fallback till `e.name` om `videogame` saknas (t.ex. custom events utan
kopplat spel pa Start.gg).

### 3. Uppdatera postgres_api.py — normalisera vid skrivning

I `shared/postgres_api.py` (rad ~2349), normalisera spelnamnet innan det
laggs till i `game_counts`. Skapa en delad funktion:

**Ny fil: `shared/game_utils.py`**

```python
"""Canonical game name normalization."""
import re

# Mapping: alla kanda varianter -> kanoniskt namn
# Dessa bor matcha videogame.name fran Start.gg
GAME_ALIASES = {
    "ssbu": "Super Smash Bros. Ultimate",
    "smash singles": "Super Smash Bros. Ultimate",
    "super smash bros ultimate": "Super Smash Bros. Ultimate",
    "super smash bros ultimate - singles": "Super Smash Bros. Ultimate",
    "super smash bros. ultimate": "Super Smash Bros. Ultimate",
    "super smash bros. ultimate - singles": "Super Smash Bros. Ultimate",
    "sf6": "Street Fighter 6",
    "street fighter 6": "Street Fighter 6",
    "street fighter 6 tournament": "Street Fighter 6",
    "t8": "Tekken 8",
    "tekken 8": "Tekken 8",
    "tekken 8 tournament": "Tekken 8",
}

def normalize_game_name(name: str) -> str:
    """Normalize a game name to its canonical form.

    Uses Start.gg's videogame names as canonical forms.
    Falls back to the original name if no alias found.
    """
    if not name:
        return ""
    key = name.strip().lower()
    # Exact match
    if key in GAME_ALIASES:
        return GAME_ALIASES[key]
    # Fuzzy: strip suffixes like " - singles", " tournament"
    compact = re.sub(r"\s*[-:]\s*(singles|doubles|tournament|bracket)$", "", key)
    if compact in GAME_ALIASES:
        return GAME_ALIASES[compact]
    return name.strip()
```

Anvand denna i `postgres_api.py` dar `game_counts` uppdateras:

```python
from shared.game_utils import normalize_game_name

# Rad ~2349 (existing player update)
for g in new_games:
    canonical = normalize_game_name(g)
    p_game_counts[canonical] = p_game_counts.get(canonical, 0) + 1

# Rad ~2419 (new player create)
for g in new_games:
    canonical = normalize_game_name(g)
    game_counts[canonical] = game_counts.get(canonical, 0) + 1
```

### 4. Konsolidera befintliga normaliseringsfunktioner

Tre stallen har idag egen normaliseringslogik:

| Fil | Funktion | Anvandning |
|-----|----------|------------|
| `backend/main.py:198` | `expand_game_display_name()` | API-svar |
| `fgt_dashboard/callbacks.py:402` | `shorten_game()` | Dashboard UI |
| `fgt_dashboard/callbacks.py:1674` | `_normalize_game_name()` | Insights analytics |

Alla tre bor importera fran `shared/game_utils.py` istallet for att ha
egen logik. `shorten_game()` kan finnas kvar som UI-helper men bor anvanda
`normalize_game_name()` som bas.

### 5. Engangsmigration av befintlig data

Kor ett script som normaliserar alla befintliga `game_counts` i `players`:

```python
"""One-time migration: normalize game_counts in players table."""
from shared.game_utils import normalize_game_name

# For varje spelare:
# 1. Las game_counts (JSON dict)
# 2. Sla ihop varianter under kanoniskt namn
# 3. Rakna om favorite_game
# 4. Uppdatera players-raden

# Pseudokod:
for player in all_players:
    old_counts = player["game_counts"] or {}
    new_counts = {}
    for game, count in old_counts.items():
        canonical = normalize_game_name(game)
        new_counts[canonical] = new_counts.get(canonical, 0) + count
    new_favorite = max(new_counts, key=new_counts.get) if new_counts else None

    UPDATE players SET
        game_counts = new_counts,
        favorite_game = new_favorite
    WHERE uuid = player["uuid"]
```

**Kor migrationen pa bade dev och prod.**

## Nuvarande data (Viktor Molina som exempel)

```
game_counts: {
    "SMASH SINGLES": 2,
    "TEKKEN 8 TOURNAMENT": 3,
    "STREET FIGHTER 6 TOURNAMENT": 7,
    "SUPER SMASH BROS ULTIMATE - SINGLES": 5
}
favorite_game: "STREET FIGHTER 6 TOURNAMENT"
```

**Efter migrering:**

```
game_counts: {
    "Super Smash Bros. Ultimate": 7,
    "Tekken 8": 3,
    "Street Fighter 6": 7
}
favorite_game: "Super Smash Bros. Ultimate"  (eller SF6, lika — ta forsta)
```

## Verifiering

```sql
-- Kolla att inga gamla varianter finns kvar
SELECT DISTINCT jsonb_object_keys(game_counts::jsonb) AS game
FROM players
WHERE game_counts IS NOT NULL
ORDER BY game;

-- Forvantat: bara kanoniska namn (Super Smash Bros. Ultimate, Street Fighter 6, Tekken 8)
```

## Klart-kriterier

- [ ] GraphQL-query uppdaterad med `videogame { name }`
- [ ] Parse Response anvander `videogame.name`
- [ ] `shared/game_utils.py` skapad med `normalize_game_name()`
- [ ] `postgres_api.py` normaliserar vid skrivning till `game_counts`
- [ ] Befintliga normaliseringsfunktioner konsoliderade
- [ ] Migrationsscript kort pa dev-databasen
- [ ] Migrationsscript kort pa prod-databasen
- [ ] `favorite_game` korrekt for alla spelare
- [ ] Inga dubbletter i `game_counts`-nycklar
