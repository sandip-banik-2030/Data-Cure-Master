import sqlite3
import os
import base64
from flask import g


class PgWrapper:
    """Wrapper to make psycopg2 connection act identically to sqlite3.Connection in Flask."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        # Convert SQLite '?' placeholders to PostgreSQL '%s' placeholders
        sql_pg = sql.replace("?", "%s")
        cur = self.conn.cursor()
        cur.execute(sql_pg, params)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "datacure.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_db():
    if "db" not in g:
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            # Connect to Supabase Cloud PostgreSQL on Render
            import psycopg2
            from psycopg2.extras import DictCursor

            raw_conn = psycopg2.connect(db_url, cursor_factory=DictCursor)
            raw_conn.autocommit = True
            g.db = PgWrapper(raw_conn)
        else:
            # Fallback to local SQLite when testing in Replit
            conn = sqlite3.connect(os.path.abspath(DB_PATH))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            g.db = conn
    return g.db


def init_db(app):
    # Skip SQLite setup on Render because tables live in Supabase
    if os.environ.get("DATABASE_URL"):
        return

    with app.app_context():
        db = sqlite3.connect(os.path.abspath(DB_PATH))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        with open(SCHEMA_PATH, "r") as f:
            db.executescript(f.read())
        _migrate(db)
        db.commit()
        db.close()


def _migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    user_migrations = [
        ("streak_bonus_date", "ALTER TABLE users ADD COLUMN streak_bonus_date TEXT"),
        (
            "lifetime_data_saved",
            "ALTER TABLE users ADD COLUMN lifetime_data_saved INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "total_ads_watched",
            "ALTER TABLE users ADD COLUMN total_ads_watched INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "is_admin",
            "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "daily_data_target",
            "ALTER TABLE users ADD COLUMN daily_data_target INTEGER NOT NULL DEFAULT 200",
        ),
        (
            "today_data_saved",
            "ALTER TABLE users ADD COLUMN today_data_saved INTEGER NOT NULL DEFAULT 0",
        ),
        ("last_data_date", "ALTER TABLE users ADD COLUMN last_data_date TEXT"),
        ("target_bonus_date", "ALTER TABLE users ADD COLUMN target_bonus_date TEXT"),
    ]
    for col, sql in user_migrations:
        if col not in cols:
            conn.execute(sql)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS security_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            event_type  TEXT    NOT NULL,
            details     TEXT    NOT NULL DEFAULT '',
            ip_address  TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_logs_type ON security_logs(event_type, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_logs_user ON security_logs(user_id, created_at DESC)"
    )

    rows = conn.execute("SELECT id, name, phone_encrypted FROM users").fetchall()
    for row in rows:
        expected = base64.b64encode(row[1].encode()).decode()
        if row[2] != expected:
            conn.execute(
                "UPDATE users SET phone_encrypted=? WHERE id=?", (expected, row[0])
            )

    conn.commit()
