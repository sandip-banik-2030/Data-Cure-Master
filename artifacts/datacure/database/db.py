import sqlite3
import os
from flask import g

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "datacure.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(os.path.abspath(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def init_db(app):
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
    migrations = [
        ("streak_bonus_date", "ALTER TABLE users ADD COLUMN streak_bonus_date TEXT"),
        ("lifetime_data_saved", "ALTER TABLE users ADD COLUMN lifetime_data_saved INTEGER NOT NULL DEFAULT 0"),
        ("total_ads_watched", "ALTER TABLE users ADD COLUMN total_ads_watched INTEGER NOT NULL DEFAULT 0"),
        ("is_admin", "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, sql in migrations:
        if col not in cols:
            conn.execute(sql)
    conn.commit()
