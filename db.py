"""db.py — dual-backend database layer for Module 2 (SQLite locally, Postgres on
Railway). The point: pl_db / pl_cogs / pl_tracker / pl_ads / pl_postage / pl_price
/ pl_amazon keep their existing `?`-placeholder SQL almost unchanged, and this
layer makes it run on Postgres too.

How it decides which backend:
  * if DATABASE_URL (or a url passed to connect()) is set -> Postgres (psycopg2)
  * else -> SQLite at the given path (current local behaviour, unchanged)

What the wrapper does on the Postgres path (and NOT on SQLite):
  * translates `?` placeholders  -> `%s`   (psycopg2's paramstyle)
  * doubles literal `%`          -> `%%`   (so LIKE '%x%' survives psycopg2)
  * returns dict-accessible rows (RealDictRow) so row["col"] works like sqlite3.Row
  * splits executescript() into individual statements (psycopg2 has no equivalent)

Upserts already use `ON CONFLICT` in the pl_* code (Postgres-compatible). The only
SQLite-isms that still need per-file hand-porting are `INSERT OR IGNORE`
(-> ON CONFLICT DO NOTHING), `PRAGMA table_info` (-> table_columns() below), and a
few `CREATE TABLE` type choices (money -> NUMERIC(14,4)); helpers for those are here.

READ/WRITE: this is the app's real DB access (unlike collector_ro, which is a
separate read-only window into Module 1's collector). Nothing here touches the
collector.
"""
import os
import re
import sqlite3

try:
    import psycopg2
    import psycopg2.extras
    _HAVE_PG = True
except Exception:
    _HAVE_PG = False


def _pg_url(explicit=None):
    return explicit or os.environ.get("DATABASE_URL") or ""


def is_postgres(explicit_url=None):
    return bool(_pg_url(explicit_url)) and _HAVE_PG


# translate a `?`-placeholder, sqlite-style SQL string to psycopg2 style.
# order matters: double the %, THEN swap ? -> %s.
def _to_pg_sql(sql):
    return sql.replace("%", "%%").replace("?", "%s")


class _PGCursor:
    """Wraps a psycopg2 cursor so .execute()/.fetchone()/.fetchall() behave like
    sqlite3's, including `?` placeholders and dict-row access."""
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(_to_pg_sql(sql), params)
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(_to_pg_sql(sql), list(seq))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # psycopg2 has no lastrowid; callers that need it use RETURNING instead.
        return None

    def close(self):
        self._cur.close()


class _PGConn:
    """sqlite3.Connection-compatible facade over a psycopg2 connection."""
    def __init__(self, url):
        # Railway requires TLS (default); a local Postgres has no SSL, so allow an
        # override via DB_SSLMODE=disable for local testing. Production leaves it unset.
        self._c = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor,
                                   connect_timeout=15,
                                   sslmode=os.environ.get("DB_SSLMODE", "require"))
        self._c.autocommit = False

    def execute(self, sql, params=()):
        cur = _PGCursor(self._c.cursor())
        return cur.execute(sql, params)

    def executemany(self, sql, seq):
        return _PGCursor(self._c.cursor()).executemany(sql, seq)

    def executescript(self, script):
        # psycopg2 can run multiple statements in one execute() as long as none
        # return rows; but to match sqlite semantics we split and run each.
        cur = self._c.cursor()
        for stmt in _split_statements(script):
            if stmt.strip():
                cur.execute(_to_pg_sql(stmt))
        cur.close()

    def cursor(self):
        return _PGCursor(self._c.cursor())

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()

    # context-manager parity with sqlite3 (commit on clean exit)
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._c.commit()
        else:
            self._c.rollback()
        return False


def _split_statements(script):
    """Naive-but-safe split of a schema script on `;` at statement end. Our schema
    scripts are plain DDL (no PL/pgSQL bodies with inner semicolons), so this holds."""
    return [s for s in re.split(r";\s*\n", script) if s.strip()]


def connect(db_path="bsr_history.db", url=None):
    """The single entry point. Returns a connection whose .execute() takes
    `?`-placeholder SQL on BOTH backends."""
    if is_postgres(url):
        return _PGConn(_pg_url(url))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── schema-introspection helper (replaces `PRAGMA table_info`) ───────────────
def table_columns(conn, table):
    """Set of column names for `table`, on either backend."""
    if isinstance(conn, _PGConn):
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=?", (table,)).fetchall()
        return {r["column_name"] for r in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def table_exists(conn, table):
    if isinstance(conn, _PGConn):
        r = conn.execute("SELECT to_regclass(?) AS t", (f"public.{table}",)).fetchone()
        return bool(r and r["t"])
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(r)
