"""Small transactional SQLite store; no payment or installation secrets in plaintext."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, username TEXT, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS apps (
 slug TEXT PRIMARY KEY, title TEXT NOT NULL, version TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 available INTEGER NOT NULL CHECK(available IN (0,1))
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), app_slug TEXT NOT NULL,
 app_title TEXT NOT NULL, amount_rub TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'RUB',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','canceled','refunded')),
 request_id TEXT NOT NULL, payment_id TEXT UNIQUE, payment_url TEXT,
 created_at INTEGER NOT NULL, paid_at INTEGER, checked_at INTEGER NOT NULL DEFAULT 0,
 test INTEGER NOT NULL CHECK(test IN (0,1)), UNIQUE(user_id, request_id)
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created_at);
CREATE TABLE IF NOT EXISTS install_grants (
 token_hash TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), expires_at INTEGER NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))
);
CREATE INDEX IF NOT EXISTS grants_expiry ON install_grants(expires_at);
'''


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self, *, write=False):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()
