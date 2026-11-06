"""Locked, file-based FGT schema migrations with an auditable application history."""

import argparse
import hashlib
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"
LOCK_ID = int.from_bytes(hashlib.sha256(b"fgt-checkin-migrations").digest()[:8], "big", signed=True)
_FILENAME = re.compile(r"(?P<version>[0-9]{3})_[a-zA-Z][a-zA-Z0-9_-]*\.sql")
_ALLOW_DESTRUCTIVE = re.compile(r"-- migrate:allow-destructive\s+(\S.*)")
_DOLLAR_QUOTE = re.compile(r"\$(?:[a-zA-Z_][a-zA-Z0-9_]*)?\$")
_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[a-zA-Z_][a-zA-Z0-9_$]*)'
_ALTER_TABLE = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    rf"(?P<table>{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})*)",
    re.IGNORECASE,
)
_FORBIDDEN = (
    (r"\bDROP\b", "DROP"),
    (r"\bTRUNCATE\b", "TRUNCATE"),
    (r"\bDELETE\s+FROM\b", "DELETE FROM"),
    (r"\bRENAME\b", "RENAME"),
    (
        rf"\bALTER\s+(?:COLUMN\s+)?{_IDENTIFIER}\s+(?:SET\s+DATA\s+)?TYPE\b",
        "ALTER COLUMN TYPE",
    ),
)


class MigrationError(RuntimeError):
    """An invalid migration or a failed migration operation."""


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    sha256: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    version: str
    filename: str
    sha256: str
    applied_at: datetime | None = None


def _strip_comments(source: str, mask_literals: bool = False) -> str:
    """Strip SQL comments without treating quoted comment markers as comments.

    Dollar bodies are checked recursively, including SQL inside DO blocks. Quoted
    text stays visible to the conservative destructive-keyword checks.
    """
    result = []
    index = 0
    while index < len(source):
        if source.startswith("--", index):
            end = source.find("\n", index)
            index = len(source) if end == -1 else end
            result.append(" ")
        elif source.startswith("/*", index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            result.append(" ")
        elif source[index] in ("'", '"'):
            start = index
            quote = source[index]
            escaped = quote == "'" and index > 0 and source[index - 1] in "eE"
            index += 1
            while index < len(source):
                if escaped and source[index] == "\\":
                    index += 2
                elif source[index] == quote:
                    index += 1
                    if index < len(source) and source[index] == quote:
                        index += 1
                    else:
                        break
                else:
                    index += 1
            result.append(" " if mask_literals else source[start:index])
        else:
            delimiter = _DOLLAR_QUOTE.match(source, index)
            if delimiter:
                token = delimiter.group()
                start = index + len(token)
                end = source.find(token, start)
                if end != -1:
                    if mask_literals:
                        result.append(" ")
                    else:
                        result.extend((token, _strip_comments(source[start:end]), token))
                    index = end + len(token)
                    continue
            result.append(source[index])
            index += 1
    return "".join(result)


def lint(source: str, filename: str = "<migration>") -> None:
    """Reject destructive changes unless the first line explicitly explains them."""
    transaction_control = re.search(
        r"(?:^|;)\s*(?:BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|"
        r"PREPARE\s+TRANSACTION)\b",
        _strip_comments(source, mask_literals=True),
        re.IGNORECASE,
    )
    if transaction_control:
        raise MigrationError(f"{filename}: transaction boundaries belong to the migration runner")
    first_line = source.splitlines()[0] if source.splitlines() else ""
    override = _ALLOW_DESTRUCTIVE.fullmatch(first_line)
    if override:
        logger.warning("DESTRUCTIVE MIGRATION OVERRIDE: %s: %s", filename, override.group(1))
        return

    cleaned = _strip_comments(source)
    for pattern, label in _FORBIDDEN:
        if re.search(pattern, cleaned, re.IGNORECASE):
            raise MigrationError(f"{filename}: additive-only lint rejected {label}")
    for match in _ALTER_TABLE.finditer(cleaned):
        identifiers = re.findall(_IDENTIFIER, match.group("table"))
        table = identifiers[-1].strip('"').replace('""', '"').lower()
        if table.startswith(("meetup_", "card_ids")):
            raise MigrationError(f"{filename}: sibling table {table} is off-limits")


def _load_migrations(directory: Path) -> list[Migration]:
    if not directory.is_dir():
        raise MigrationError(f"Migration directory does not exist: {directory}")
    migrations = []
    versions = set()
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"{path.name}: expected NNN_<vertical>.sql")
        version = match.group("version")
        if version in versions:
            raise MigrationError(f"{path.name}: duplicate migration version {version}")
        versions.add(version)
        try:
            content = path.read_bytes()
            source = content.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise MigrationError(f"{path.name}: {error}") from error
        migrations.append(
            Migration(version, path.name, hashlib.sha256(content).hexdigest(), source)
        )
    return migrations


@contextmanager
def _connection(source):
    """Borrow a pool connection, or use an explicitly supplied psycopg connection."""
    if callable(getattr(source, "connection", None)):
        with source.connection() as connection:
            if not connection.autocommit:
                raise MigrationError("Migration runner requires an autocommit connection")
            yield connection
    else:
        if not source.autocommit:
            raise MigrationError("Migration runner requires an autocommit connection")
        yield source


def _history(connection) -> dict:
    exists = connection.execute("SELECT to_regclass('fgt_schema_migrations')").fetchone()[0]
    if exists is None:
        return {}
    rows = connection.execute(
        "SELECT version, filename, sha256, applied_at FROM fgt_schema_migrations"
    ).fetchall()
    return {row[0]: row for row in rows}


def _statuses(migrations: list[Migration], history: dict) -> list[MigrationStatus]:
    statuses = []
    for migration in migrations:
        previous = history.get(migration.version)
        if previous and (previous[1] != migration.filename or previous[2] != migration.sha256):
            raise MigrationError(f"{migration.filename}: edited after apply")
        statuses.append(
            MigrationStatus(
                migration.version,
                migration.filename,
                migration.sha256,
                previous[3] if previous else None,
            )
        )
    return statuses


def status(source, migrations_dir: Path = MIGRATIONS_DIR) -> list[MigrationStatus]:
    """Return pending/applied files, rejecting lint or checksum problems without writes."""
    migrations = _load_migrations(Path(migrations_dir))
    try:
        with _connection(source) as connection:
            states = _statuses(migrations, _history(connection))
            for migration in migrations:
                lint(migration.sql, migration.filename)
            return states
    except MigrationError:
        raise
    except Exception as error:
        raise MigrationError(f"Cannot read migration status: {error}") from error


def check(source, migrations_dir: Path = MIGRATIONS_DIR) -> list[MigrationStatus]:
    """Lint all files and list pending migrations without creating the history table."""
    return [
        migration for migration in status(source, migrations_dir) if migration.applied_at is None
    ]


def apply(source, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply each pending file in its own transaction under one session advisory lock."""
    try:
        migrations = _load_migrations(Path(migrations_dir))
        with _connection(source) as connection:
            connection.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
            try:
                statuses = _statuses(migrations, _history(connection))
                for migration in migrations:
                    lint(migration.sql, migration.filename)
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS fgt_schema_migrations ("
                    "version TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                applied = []
                for migration, state in zip(migrations, statuses):
                    if state.applied_at is not None:
                        continue
                    try:
                        with connection.transaction():
                            connection.execute(migration.sql, prepare=False)
                            connection.execute(
                                "INSERT INTO fgt_schema_migrations (version, filename, sha256) "
                                "VALUES (%s, %s, %s)",
                                (migration.version, migration.filename, migration.sha256),
                            )
                    except Exception as error:
                        raise MigrationError(f"{migration.filename}: {error}") from error
                    applied.append(migration.filename)
                    logger.info("Applied FGT migration %s", migration.filename)
                return applied
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    except Exception as error:
        logger.error("FGT file-based migrations failed: %s", error)
        if isinstance(error, MigrationError):
            raise
        raise MigrationError(f"Migration run failed: {error}") from error


def apply_if_enabled(pool) -> None:
    """Startup gate; close a failed pool before the storage hook resets its reference."""
    if os.getenv("FGT_MIGRATIONS_ENABLED", "1") == "1":
        try:
            apply(pool)
        except Exception:
            pool.close()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "apply", "check"))
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    url = os.getenv("DATABASE_URL")
    if not url:
        logger.error("DATABASE_URL is required")
        return 1
    try:
        import psycopg

        with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
            if arguments.command == "apply":
                for filename in apply(connection):
                    print(f"applied {filename}")
            else:
                states = status(connection) if arguments.command == "status" else check(connection)
                for migration in states:
                    label = "applied" if migration.applied_at is not None else "pending"
                    print(f"{label} {migration.filename}")
        return 0
    except Exception as error:
        logger.error("Migration %s failed: %s", arguments.command, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
