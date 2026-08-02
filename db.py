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
    import psycopg2.extensions
    _HAVE_PG = True
    # psycopg2 returns NUMERIC/DECIMAL as decimal.Decimal; SQLite returns REAL as
    # float, and the pl_* code does plain float arithmetic on money columns
    # (float + value). Cast NUMERIC -> float globally so both backends behave the
    # same and we never hit "unsupported operand: float + Decimal". Money here is
    # <1e6 with 4dp, well within float precision, and matches the old SQLite path.
    _DEC2FLOAT = psycopg2.extensions.new_type(
        psycopg2.extensions.DECIMAL.values, "DEC2FLOAT",
        lambda v, cur: float(v) if v is not None else None)
    psycopg2.extensions.register_type(_DEC2FLOAT)
except Exception:
    _HAVE_PG = False


def _pg_url(explicit=None):
    return explicit or os.environ.get("DATABASE_URL") or ""


def is_postgres(explicit_url=None):
    return bool(_pg_url(explicit_url)) and _HAVE_PG


# ── SQLite-only SQL function translation ─────────────────────────────────────
# The pl_* queries use a handful of SQLite built-ins that Postgres does not have.
# We rewrite them here (Postgres path only) so the app SQL stays unchanged.
#   strftime('%Y-%m-%d', col) -> to_char((col)::timestamp, 'YYYY-MM-DD')
#   julianday(x)              -> (EXTRACT(EPOCH FROM (x)::timestamp)/86400.0)
#   GROUP_CONCAT([DISTINCT] x)-> string_agg([DISTINCT] (x)::text, ',')
#   date(x) / datetime(x)     -> (x)::date / (x)::timestamp
import re as _re

_STRFTIME_CODES = {'Y': 'YYYY', 'm': 'MM', 'd': 'DD', 'H': 'HH24',
                   'M': 'MI', 'S': 'SS', 'W': 'IW', 'j': 'DDD', 'w': 'ID'}


def _strftime_fmt(fmt):
    """SQLite strftime format -> Postgres to_char template. Literal letters get
    double-quoted so to_char treats them as text, not format tokens."""
    out, lit = [], ''

    def flush():
        nonlocal lit
        if lit:
            out.append('"' + lit + '"' if any(c.isalpha() for c in lit) else lit)
            lit = ''
    i = 0
    while i < len(fmt):
        if fmt[i] == '%' and i + 1 < len(fmt):
            flush(); out.append(_STRFTIME_CODES.get(fmt[i + 1], fmt[i + 1])); i += 2
        else:
            lit += fmt[i]; i += 1
    flush()
    return ''.join(out)


def _translate_sqlite_funcs(sql):
    sql = _re.sub(r"strftime\(\s*'([^']*)'\s*,\s*([^),]+)\)",
                  lambda m: "to_char((%s)::timestamp, '%s')" % (m.group(2).strip(), _strftime_fmt(m.group(1))),
                  sql)
    sql = _re.sub(r"julianday\(\s*([^)]+?)\s*\)",
                  r"(EXTRACT(EPOCH FROM (\1)::timestamp)/86400.0)", sql)
    sql = _re.sub(r"GROUP_CONCAT\(\s*(DISTINCT\s+)?([^)]+?)\s*\)",
                  lambda m: "string_agg(%s(%s)::text, ',')" % (m.group(1) or '', m.group(2).strip()),
                  sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"\bdatetime\(\s*([^)]+?)\s*\)", r"(\1)::timestamp", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"\bdate\(\s*([^)]+?)\s*\)", r"(\1)::date", sql, flags=_re.IGNORECASE)
    return sql


# translate a `?`-placeholder, sqlite-style SQL string to psycopg2 style.
# order: SQLite-func rewrite FIRST (its output has no % or ?), THEN double the %
# (for LIKE literals), THEN swap ? -> %s.
def _to_pg_sql(sql):
    sql = _translate_sqlite_funcs(sql)
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
