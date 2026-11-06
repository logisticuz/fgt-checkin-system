"""Database destination guard and small helpers for the disposable test schema."""

from dataclasses import dataclass

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from tests.fixtures import make_checkin, make_settings

DEFAULT_TEST_DATABASE_URL = "postgresql://fgt_test:fgt_test@127.0.0.1:15432/fgt_test"
GUARD_MESSAGE = (
    "Unsafe TEST_DATABASE_URL: only 127.0.0.1:15432 or localhost:15432 is allowed; "
    "set TEST_DATABASE_ALLOW_ANY=1 only for an explicitly disposable test database."
)


def validate_test_database_url(url, allow_any=False):
    """Validate libpq's effective destination, including URI query overrides."""
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError(GUARD_MESSAGE)
    try:
        parameters = conninfo_to_dict(url)
    except Exception:
        raise ValueError(GUARD_MESSAGE) from None
    if not allow_any:
        hosts = {"127.0.0.1", "localhost"}
        if (
            parameters.get("host") not in hosts
            or parameters.get("port") != "15432"
            or parameters.get("hostaddr", "127.0.0.1") != "127.0.0.1"
            or "service" in parameters
        ):
            raise ValueError(GUARD_MESSAGE)
    return url


@dataclass
class TestDatabase:
    schema: str
    pool: object

    def insert(self, table, fields):
        values = [Jsonb(value) if isinstance(value, dict) else value for value in fields.values()]
        if "events_json" in fields:
            values[list(fields).index("events_json")] = Jsonb(fields["events_json"])
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, fields)),
            sql.SQL(", ").join(sql.Placeholder() for _ in fields),
        )
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, values)
                row = cursor.fetchone()
                return dict(zip((column.name for column in cursor.description), row))

    def seed_settings(self, **overrides):
        return self.insert("settings", make_settings(**overrides))

    def seed_checkin(self, **overrides):
        return self.insert("active_event_data", make_checkin(**overrides))

    def checkins(self):
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM active_event_data ORDER BY id")
                columns = [column.name for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
