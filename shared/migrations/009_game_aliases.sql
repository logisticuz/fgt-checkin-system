-- Read-time game-name aliases; archive and player facts are not rewritten.
-- Groups confirmed by the user; Doubles is a separate SSBU format.
CREATE TABLE IF NOT EXISTS game_aliases (
    alias TEXT PRIMARY KEY CHECK (alias = lower(btrim(alias)) AND alias <> ''),
    canonical TEXT NOT NULL CHECK (btrim(canonical) <> '')
);
INSERT INTO game_aliases (alias, canonical) VALUES
    ('street fighter 6', 'Street Fighter 6'),
    ('street fighter 6 tournament', 'Street Fighter 6'),
    ('sf6', 'Street Fighter 6'),
    ('sf6 tournament', 'Street Fighter 6'),
    ('tekken 8', 'TEKKEN 8'),
    ('tekken 8 tournament', 'TEKKEN 8'),
    ('t8', 'TEKKEN 8'),
    ('super smash bros. ultimate', 'Super Smash Bros. Ultimate'),
    ('super smash bros ultimate - singles', 'Super Smash Bros. Ultimate'),
    ('smash singles', 'Super Smash Bros. Ultimate'),
    ('ssbu singles', 'Super Smash Bros. Ultimate'),
    ('ssbu', 'Super Smash Bros. Ultimate'),
    ('guilty gear -strive-', 'Guilty Gear -Strive-'),
    ('guilty gear strive', 'Guilty Gear -Strive-'),
    ('guilty gear strive tournament', 'Guilty Gear -Strive-'),
    ('super smash bros. ultimate doubles', 'Super Smash Bros. Ultimate Doubles'),
    ('doubles', 'Super Smash Bros. Ultimate Doubles')
ON CONFLICT (alias) DO NOTHING;
