"""SQLite store in WAL mode.

One writer: the scheduler and the runner both go through the lock below.
Request handlers only read, each thread on a connection of its own, because
a sqlite3 connection cannot be stepped from two threads at once. Timestamps
are UTC integers.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_runs (
    source TEXT NOT NULL,
    ts INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    error TEXT,
    skipped INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS source_runs_source_ts ON source_runs (source, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    ts INTEGER NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS samples (
    series TEXT NOT NULL,
    ts INTEGER NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_series_ts ON samples (series, ts);

CREATE TABLE IF NOT EXISTS speedtests (
    instance TEXT NOT NULL,
    id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    download REAL,
    upload REAL,
    ping REAL,
    status TEXT,
    server TEXT,
    PRIMARY KEY (instance, id)
);
CREATE INDEX IF NOT EXISTS speedtests_instance_created ON speedtests (instance, created_at);

CREATE TABLE IF NOT EXISTS upstream_threads (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    comments INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL,
    author TEXT,
    changed_ts INTEGER,
    seen_ts INTEGER NOT NULL,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS upstream_releases (
    repo TEXT PRIMARY KEY,
    latest_tag TEXT,
    published_at TEXT,
    url TEXT,
    running_version TEXT,
    running_source TEXT,
    seen_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    cleared_ts INTEGER,
    ack TEXT,
    acked_ts INTEGER
);
CREATE INDEX IF NOT EXISTS attention_cleared ON attention (cleared_ts);

CREATE TABLE IF NOT EXISTS action_runs (
    id INTEGER PRIMARY KEY,
    action_id TEXT NOT NULL,
    target TEXT NOT NULL,
    summary TEXT NOT NULL,
    started_ts INTEGER NOT NULL,
    finished_ts INTEGER,
    exit_code INTEGER,
    requested_by TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,
    output_file TEXT
);
"""

# columns added after the first release; CREATE TABLE IF NOT EXISTS skips them
# on an existing file
MIGRATIONS = (
    ("attention", "ack", "TEXT"),
    ("attention", "acked_ts", "INTEGER"),
)
SEVERITY_RANK = {"crit": 0, "warn": 1, "info": 2}

DAY = 86400
RETENTION = {
    "samples": 90 * DAY,
    "speedtests": 365 * DAY,
    "source_runs": 7 * DAY,
    "attention": 30 * DAY,
}


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = self._connect()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            present = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _reader(self) -> sqlite3.Connection:
        # an in-memory database is only reachable through the connection
        # that created it
        if self.path == ":memory:":
            return self.conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
            with self.lock:
                self._readers.append(conn)
        return conn

    def close(self):
        with self.lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        self.conn.close()

    def _write(self, sql: str, params=()):
        with self.lock:
            self.conn.execute(sql, params)

    def _writemany(self, sql: str, rows):
        rows = list(rows)
        if not rows:
            return
        with self.lock:
            self.conn.executemany(sql, rows)

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self._reader().execute(sql, params).fetchall()

    def _one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self._reader().execute(sql, params).fetchone()

    # source runs

    def record_run(self, source: str, ok: bool, duration_ms: int, error: str | None = None,
                   skipped: bool = False, ts: int | None = None):
        self._write(
            "INSERT INTO source_runs (source, ts, ok, duration_ms, error, skipped)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (source, ts or now(), int(ok), duration_ms, error, int(skipped)),
        )

    def last_run(self, source: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM source_runs WHERE source = ? ORDER BY ts DESC LIMIT 1", (source,)
        )

    def last_ok_ts(self, source: str) -> int | None:
        row = self._one(
            "SELECT MAX(ts) AS ts FROM source_runs WHERE source = ? AND ok = 1", (source,)
        )
        return row["ts"] if row else None

    def source_status(self) -> dict[str, dict]:
        """Latest run per source plus the time of its last success."""
        out = {}
        for r in self._query(
            "SELECT s.source, s.ts, s.ok, s.error, s.skipped, s.duration_ms,"
            " (SELECT MAX(ts) FROM source_runs o WHERE o.source = s.source AND o.ok = 1)"
            " AS last_ok"
            " FROM source_runs s"
            " WHERE s.ts = (SELECT MAX(ts) FROM source_runs m WHERE m.source = s.source)"
        ):
            out[r["source"]] = dict(r)
        return out

    def error_streak_since(self, source: str) -> int | None:
        """Timestamp of the first error in the current unbroken streak, if any."""
        rows = self._query(
            "SELECT ts, ok, skipped FROM source_runs WHERE source = ? ORDER BY ts DESC LIMIT 200",
            (source,),
        )
        since = None
        for r in rows:
            if r["skipped"]:
                continue
            if r["ok"]:
                break
            since = r["ts"]
        return since

    # snapshots

    def put_snapshot(self, source: str, key: str, obj, ts: int | None = None):
        self._write(
            "INSERT OR REPLACE INTO snapshots (source, key, ts, json) VALUES (?, ?, ?, ?)",
            (source, key, ts or now(), json.dumps(obj, separators=(",", ":"))),
        )

    def put_snapshots(self, source: str, items: dict[str, object], prefix: str | None = None,
                      ts: int | None = None):
        """Replace the keys under prefix with items; keys no longer present go away."""
        ts = ts or now()
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                if prefix is None:
                    rows = self.conn.execute(
                        "SELECT key FROM snapshots WHERE source = ?", (source,)
                    ).fetchall()
                else:
                    rows = self.conn.execute(
                        "SELECT key FROM snapshots WHERE source = ? AND substr(key, 1, ?) = ?",
                        (source, len(prefix), prefix),
                    ).fetchall()
                stale = [(source, r["key"]) for r in rows if r["key"] not in items]
                self.conn.executemany(
                    "DELETE FROM snapshots WHERE source = ? AND key = ?", stale
                )
                self.conn.executemany(
                    "INSERT OR REPLACE INTO snapshots (source, key, ts, json) VALUES (?, ?, ?, ?)",
                    [(source, k, ts, json.dumps(v, separators=(",", ":")))
                     for k, v in items.items()],
                )
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def get_snapshot(self, source: str, key: str):
        row = self._one(
            "SELECT json FROM snapshots WHERE source = ? AND key = ?", (source, key)
        )
        return json.loads(row["json"]) if row else None

    def get_snapshots(self, source: str, prefix: str | None = None) -> list[tuple[str, int, dict]]:
        if prefix is None:
            rows = self._query(
                "SELECT key, ts, json FROM snapshots WHERE source = ? ORDER BY key", (source,)
            )
        else:
            rows = self._query(
                "SELECT key, ts, json FROM snapshots WHERE source = ? AND substr(key, 1, ?) = ?"
                " ORDER BY key",
                (source, len(prefix), prefix),
            )
        return [(r["key"], r["ts"], json.loads(r["json"])) for r in rows]

    # samples

    def add_samples(self, rows):
        self._writemany("INSERT INTO samples (series, ts, value) VALUES (?, ?, ?)", rows)

    def series(self, series: str, since: int) -> list[tuple[int, float]]:
        return [
            (r["ts"], r["value"])
            for r in self._query(
                "SELECT ts, value FROM samples WHERE series = ? AND ts >= ? ORDER BY ts",
                (series, since),
            )
        ]

    def latest_sample(self, series: str) -> tuple[int, float] | None:
        r = self._one(
            "SELECT ts, value FROM samples WHERE series = ? ORDER BY ts DESC LIMIT 1", (series,)
        )
        return (r["ts"], r["value"]) if r else None

    # speedtests

    def upsert_speedtests(self, instance: str, rows):
        self._writemany(
            "INSERT OR REPLACE INTO speedtests"
            " (instance, id, created_at, download, upload, ping, status, server)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(instance, *r) for r in rows],
        )

    def last_speedtest_ts(self, instance: str) -> int | None:
        r = self._one(
            "SELECT MAX(created_at) AS ts FROM speedtests WHERE instance = ?", (instance,)
        )
        return r["ts"] if r else None

    def speedtests(self, instance: str, since: int) -> list[dict]:
        return [
            dict(r)
            for r in self._query(
                "SELECT id, created_at, download, upload, ping, status, server FROM speedtests"
                " WHERE instance = ? AND created_at >= ? ORDER BY created_at",
                (instance, since),
            )
        ]

    # upstream

    def upsert_thread(self, repo: str, number: int, kind: str, title: str, state: str,
                      updated_at: str, comments: int, url: str, author: str | None,
                      ts: int | None = None) -> bool:
        """Store the thread; returns True when updated_at moved since the last poll."""
        ts = ts or now()
        with self.lock:
            old = self.conn.execute(
                "SELECT updated_at, changed_ts FROM upstream_threads WHERE repo = ? AND number = ?",
                (repo, number),
            ).fetchone()
            changed = old is not None and old["updated_at"] != updated_at
            changed_ts = ts if changed else (old["changed_ts"] if old else None)
            self.conn.execute(
                "INSERT OR REPLACE INTO upstream_threads"
                " (repo, number, kind, title, state, updated_at, comments, url, author,"
                " changed_ts, seen_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, number, kind, title, state, updated_at, comments, url, author,
                 changed_ts, ts),
            )
        return changed

    def threads(self) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM upstream_threads ORDER BY repo, number"
        )]

    def upsert_release(self, repo: str, latest_tag: str | None, published_at: str | None,
                       url: str | None, running_version: str | None,
                       running_source: str | None, ts: int | None = None):
        self._write(
            "INSERT OR REPLACE INTO upstream_releases"
            " (repo, latest_tag, published_at, url, running_version, running_source, seen_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo, latest_tag, published_at, url, running_version, running_source, ts or now()),
        )

    def releases(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM upstream_releases ORDER BY repo")]

    # attention

    def sync_attention(self, active: dict[tuple[str, str], dict], ts: int | None = None):
        """Open rows for new items, refresh the ones still active, clear the rest.

        Returns (opened, cleared) as lists of (source, key). A marked item
        whose severity got worse loses its mark and counts as opened."""
        ts = ts or now()
        opened, cleared = [], []
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                rows = self.conn.execute(
                    "SELECT id, source, key, severity, ack FROM attention"
                    " WHERE cleared_ts IS NULL"
                ).fetchall()
                seen = set()
                for r in rows:
                    k = (r["source"], r["key"])
                    seen.add(k)
                    item = active.get(k)
                    if item is None:
                        self.conn.execute(
                            "UPDATE attention SET cleared_ts = ? WHERE id = ?", (ts, r["id"])
                        )
                        cleared.append(k)
                    else:
                        sets = "severity = ?, title = ?, detail = ?, last_seen = ?"
                        worse = (SEVERITY_RANK.get(item["severity"], 3)
                                 < SEVERITY_RANK.get(r["severity"], 3))
                        if r["ack"] and worse:
                            sets += ", ack = NULL, acked_ts = NULL"
                            opened.append(k)
                        self.conn.execute(
                            f"UPDATE attention SET {sets} WHERE id = ?",
                            (item["severity"], item["title"], item.get("detail"), ts, r["id"]),
                        )
                for k, item in active.items():
                    if k in seen:
                        continue
                    self.conn.execute(
                        "INSERT INTO attention"
                        " (source, key, severity, title, detail, first_seen, last_seen)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (k[0], k[1], item["severity"], item["title"], item.get("detail"),
                         item.get("first_seen", ts), ts),
                    )
                    opened.append(k)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return opened, cleared

    def open_attention(self) -> list[dict]:
        order = "CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 WHEN 'info' THEN 2 ELSE 3 END"
        return [dict(r) for r in self._query(
            f"SELECT * FROM attention WHERE cleared_ts IS NULL ORDER BY {order}, first_seen"
        )]

    def unacked_attention(self) -> list[dict]:
        return [a for a in self.open_attention() if not a["ack"]]

    def ack_attention(self, item_id: int, kind: str | None, ts: int | None = None) -> bool:
        """Mark an open item read or resolved; None takes the mark off again."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE attention SET ack = ?, acked_ts = ? WHERE id = ? AND cleared_ts IS NULL",
                (kind, (ts or now()) if kind else None, item_id),
            )
            return cur.rowcount == 1

    # action runs

    def new_run(self, action_id: str, target: str, summary: str, requested_by: str | None,
                confirmed: bool, output_file: str, ts: int | None = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO action_runs"
                " (action_id, target, summary, started_ts, requested_by, confirmed, output_file)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action_id, target, summary, ts or now(), requested_by, int(confirmed),
                 output_file),
            )
            return cur.lastrowid

    def set_run_output(self, run_id: int, output_file: str):
        self._write("UPDATE action_runs SET output_file = ? WHERE id = ?", (output_file, run_id))

    def finish_run(self, run_id: int, exit_code: int, ts: int | None = None):
        self._write(
            "UPDATE action_runs SET finished_ts = ?, exit_code = ? WHERE id = ?",
            (ts or now(), exit_code, run_id),
        )

    def run(self, run_id: int) -> dict | None:
        r = self._one("SELECT * FROM action_runs WHERE id = ?", (run_id,))
        return dict(r) if r else None

    def runs(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM action_runs ORDER BY id DESC LIMIT ?", (limit,)
        )]

    def runs_for_target(self, target: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM action_runs WHERE target = ? ORDER BY id DESC LIMIT ?",
            (target, limit),
        )]

    def last_run_per_action(self) -> dict[str, dict]:
        out = {}
        for r in self._query(
            "SELECT * FROM action_runs a WHERE id = (SELECT MAX(id) FROM action_runs b"
            " WHERE b.action_id = a.action_id)"
        ):
            out[r["action_id"]] = dict(r)
        return out

    # housekeeping

    def prune(self, ts: int | None = None):
        ts = ts or now()
        with self.lock:
            self.conn.execute("DELETE FROM samples WHERE ts < ?", (ts - RETENTION["samples"],))
            self.conn.execute(
                "DELETE FROM speedtests WHERE created_at < ?", (ts - RETENTION["speedtests"],)
            )
            self.conn.execute(
                "DELETE FROM source_runs WHERE ts < ?", (ts - RETENTION["source_runs"],)
            )
            self.conn.execute(
                "DELETE FROM attention WHERE cleared_ts IS NOT NULL AND cleared_ts < ?",
                (ts - RETENTION["attention"],),
            )
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def vacuum(self):
        with self.lock:
            self.conn.execute("VACUUM")

    def size_bytes(self) -> int:
        if self.path == ":memory:":
            return 0
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0
