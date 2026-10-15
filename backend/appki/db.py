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
CREATE TABLE IF NOT EXISTS order_items (
 order_id TEXT NOT NULL REFERENCES orders(id), app_slug TEXT NOT NULL,
 app_title TEXT NOT NULL, amount_rub TEXT NOT NULL,
 installed_at INTEGER, activated_at INTEGER, artifact_id TEXT,
 PRIMARY KEY(order_id, app_slug), CHECK(activated_at IS NULL OR installed_at IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS install_grants (
 token_hash TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), expires_at INTEGER NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)), app_slug TEXT NOT NULL, artifact_id TEXT
);
CREATE INDEX IF NOT EXISTS grants_expiry ON install_grants(expires_at);
CREATE TABLE IF NOT EXISTS artifact_snapshots (
 id TEXT PRIMARY KEY, record_json TEXT NOT NULL,
 legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1))
);
CREATE TABLE IF NOT EXISTS activation_code_requests (
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL, app_slug TEXT NOT NULL,
 requested_at INTEGER NOT NULL,
 FOREIGN KEY(order_id,app_slug) REFERENCES order_items(order_id,app_slug)
);
CREATE INDEX IF NOT EXISTS code_requests_item ON activation_code_requests(order_id,app_slug,requested_at);
'''


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.legacy_item_migration = False

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
        with self.connection(write=True) as conn:
            # Preserve single-app orders and their existing installation links in place.
            conn.execute('''INSERT OR IGNORE INTO order_items (order_id,app_slug,app_title,amount_rub)
                SELECT id,app_slug,app_title,amount_rub FROM orders''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(install_grants)')}
            if 'app_slug' not in columns:
                conn.execute('ALTER TABLE install_grants ADD COLUMN app_slug TEXT')
            if 'artifact_id' not in columns:
                conn.execute('ALTER TABLE install_grants ADD COLUMN artifact_id TEXT')
            item_columns = {row['name'] for row in conn.execute('PRAGMA table_info(order_items)')}
            self.legacy_item_migration = 'artifact_id' not in item_columns
            if 'artifact_id' not in item_columns:
                conn.execute('ALTER TABLE order_items ADD COLUMN artifact_id TEXT')
            conn.execute('''UPDATE install_grants SET app_slug=(
                SELECT app_slug FROM orders WHERE orders.id=install_grants.order_id)
                WHERE app_slug IS NULL''')
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
