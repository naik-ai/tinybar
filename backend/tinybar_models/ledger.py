"""The request ledger: one SQLite row per gateway request, never any content.

A row holds an id (also returned in the X-Request-Id header), the client name from X-Client-Name when
given, the model, the route, the accepted time, queue, load and total milliseconds, token counts when the
worker reported them (unknown stays NULL, never 0), the HTTP status, an outcome, an error kind, the phase of
work still in flight and the ended time. No prompt, image, response, header value other than the client
name, or key is ever written.

Lifecycle: a request the gateway accepts gets its row at once, before any work is dispatched, with outcome
`in_flight` and phase `queued`; the phase moves to `loading` (its model is starting) and `generating` (the
worker has it); the row is completed in place when the request ends. When the gateway starts, rows still
`in_flight` from a previous run (it crashed or was killed) become `interrupted` with error kind
`gateway_restart`; their ended time stays NULL, never invented.

Writes: one connection, one writer at a time (a lock), WAL with synchronous=NORMAL, which survives a crash of
the gateway process (a committed row is in the WAL); only a power loss can drop the last commits.
Retention is bounded by age and by row count, pruned at start and every PRUNE_EVERY completions; rows still
in flight are never pruned.
"""

import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  client TEXT,
  model TEXT,
  route TEXT NOT NULL,
  started_at REAL NOT NULL,
  queue_ms INTEGER,
  load_ms INTEGER,
  duration_ms INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  status INTEGER,
  outcome TEXT NOT NULL,
  error_kind TEXT
);
CREATE INDEX IF NOT EXISTS requests_started ON requests (started_at);
"""
ADDED_COLUMNS = {"phase": "TEXT", "ended_at": "REAL"}  # added to ledgers written before them
COLUMNS = ("id", "client", "model", "route", "started_at", "queue_ms", "load_ms", "duration_ms", "prompt_tokens",
           "completion_tokens", "status", "outcome", "error_kind", "phase", "ended_at")
OUTCOMES = {"ok", "error", "refused", "cancelled", "not_available", "in_flight", "interrupted", "partial"}
PHASES = {"queued", "loading", "generating"}
CLIENT = re.compile(r"[^A-Za-z0-9._:/@-]")
PRUNE_EVERY = 200


def new_id() -> str:
    return "req_" + uuid.uuid4().hex


def client_name(header: str | None) -> str | None:
    """A short, plain label: anything outside [A-Za-z0-9._:/@-] is dropped, 64 characters at most."""
    if not header:
        return None
    clean = CLIENT.sub("", header)[:64]
    return clean or None


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


class Ledger:
    def __init__(self, path: Path, max_age_days: float = 30, max_rows: int = 100_000, clock=time.time):
        self.path, self.max_age_days, self.max_rows, self.clock = Path(path), max_age_days, max_rows, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        have = {r[1] for r in self._db.execute("PRAGMA table_info(requests)")}
        for column, kind in ADDED_COLUMNS.items():
            if column not in have:
                self._db.execute(f"ALTER TABLE requests ADD COLUMN {column} {kind}")
        self._db.execute("CREATE INDEX IF NOT EXISTS requests_outcome ON requests (outcome)")
        self._inserts = 0
        self._closed = False
        self.prune()

    def _write(self, sql: str, args: tuple) -> int:
        with self._lock:
            if self._closed:
                return 0
            return self._db.execute(sql, args).rowcount

    def start(self, *, id: str, route: str, started_at: float, client: str | None = None, model: str | None = None,
              phase: str = "queued") -> None:
        """The row of a request just accepted: outcome in_flight, before any work is dispatched."""
        self._write("INSERT OR REPLACE INTO requests (id, client, model, route, started_at, outcome, phase) "
                    "VALUES (?, ?, ?, ?, ?, 'in_flight', ?)",
                    (id, client_name(client), model, route, float(started_at), phase))

    def update(self, id: str, *, phase: str | None = None, model: str | None = None) -> None:
        """Move an in-flight row to its next phase, or name its model; a completed row is never changed back."""
        if phase is not None and phase not in PHASES:
            raise ValueError(f"phase must be one of {sorted(PHASES)}")
        sets, args = [], []
        if phase is not None:
            sets.append("phase = ?")
            args.append(phase)
        if model is not None:
            sets.append("model = ?")
            args.append(model)
        if sets:
            self._write(f"UPDATE requests SET {', '.join(sets)} WHERE id = ? AND outcome = 'in_flight'", (*args, id))

    def add(self, *, id: str, route: str, started_at: float, outcome: str, client: str | None = None,
            model: str | None = None, queue_ms=None, load_ms=None, duration_ms=None, prompt_tokens=None,
            completion_tokens=None, status: int | None = None, error_kind: str | None = None,
            ended_at: float | None = None) -> None:
        """The completed row (it replaces the request's in-flight row, under the same id)."""
        if outcome not in OUTCOMES - {"in_flight", "interrupted"}:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES - {'in_flight', 'interrupted'})}")
        row = (id, client_name(client), model, route, float(started_at), _int_or_none(queue_ms), _int_or_none(load_ms),
               _int_or_none(duration_ms), _int_or_none(prompt_tokens), _int_or_none(completion_tokens),
               status, outcome, error_kind, None, self.clock() if ended_at is None else float(ended_at))
        self._write(f"INSERT OR REPLACE INTO requests ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})", row)
        with self._lock:
            self._inserts += 1
            due = self._inserts % PRUNE_EVERY == 0
        if due:
            self.prune()

    def reconcile(self) -> int:
        """At gateway start: rows still in flight belong to a run that did not finish them. They become
        interrupted (gateway_restart); their ended time stays unknown. Returns how many."""
        return self._write("UPDATE requests SET outcome = 'interrupted', error_kind = 'gateway_restart' "
                           "WHERE outcome = 'in_flight'", ())

    def recent(self, limit: int = 100, model: str | None = None, outcome: str | None = None,
               before: float | None = None) -> list[dict]:
        """Newest first. Filters: model, outcome; `before`: only rows accepted earlier (a cursor: pass the last
        row's started_at to read the next page). In-flight rows carry elapsed_ms (now minus accepted)."""
        limit = max(1, min(int(limit), 1000))
        where, args = [], []
        for column, value in (("model", model), ("outcome", outcome)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if before is not None:
            where.append("started_at < ?")
            args.append(float(before))
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(COLUMNS)} FROM requests {clause}ORDER BY started_at DESC LIMIT ?",
                                    (*args, limit)).fetchall()
        now = self.clock()
        out = []
        for r in rows:
            d = dict(zip(COLUMNS, r))
            d["elapsed_ms"] = int((now - d["started_at"]) * 1000) if d["outcome"] == "in_flight" else None
            out.append(d)
        return out

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    def prune(self, now: float | None = None) -> int:
        """Drop rows older than max_age_days, then the oldest beyond max_rows; never a row still in flight."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._closed:
                return 0
            gone = self._db.execute("DELETE FROM requests WHERE started_at < ? AND outcome != 'in_flight'",
                                    (now - self.max_age_days * 86400,)).rowcount
            gone += self._db.execute(
                "DELETE FROM requests WHERE outcome != 'in_flight' AND id IN "
                "(SELECT id FROM requests ORDER BY started_at DESC LIMIT -1 OFFSET ?)", (self.max_rows,)).rowcount
        return gone

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._db.close()
