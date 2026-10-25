"""SQLite storage for Grafana.

The database lives on tmpfs (``/run/heatsteal``), so the SD card is never
written during a session. WAL mode lets Grafana read while we write;
``synchronous=OFF`` because it is RAM anyway. Rows are buffered and committed
in batches every ``commit_s`` seconds.

Only the service's control thread writes. Readers (the web API, Grafana)
open their own connections.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .model import Event, RelayOutputs, Snapshot, State

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL);
CREATE TABLE IF NOT EXISTS readings (ts REAL NOT NULL, sensor TEXT NOT NULL, celsius REAL NOT NULL);
CREATE TABLE IF NOT EXISTS power    (ts REAL NOT NULL, loop TEXT NOT NULL, watts REAL NOT NULL, joules REAL NOT NULL);
CREATE TABLE IF NOT EXISTS relays   (ts REAL NOT NULL, peltier INTEGER, cold_pump INTEGER, hot_pump INTEGER,
                                      state TEXT);
CREATE TABLE IF NOT EXISTS events   (ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS ix_power_ts ON power(ts);
"""

RETENTION_EVERY_S = 60.0


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


class Storage:
    def __init__(
        self,
        db_path: str,
        retention_hours: float = 24.0,
        archive_dir: str = "",
        commit_s: float = 5.0,
        clock=time.monotonic,
    ) -> None:
        self.db_path = db_path
        self.retention_s = retention_hours * 3600
        self.archive_dir = archive_dir
        self.commit_s = commit_s
        self._clock = clock
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._readings: list[tuple] = []
        self._power: list[tuple] = []
        self._relays: list[tuple] = []
        self._events: list[tuple] = []
        self._last_relays: tuple | None = None
        self._last_commit = clock()
        self._last_retention = clock()
        self.session_id: int | None = None
        self._archive_threads: list[threading.Thread] = []
        self._closed = False

    # -- writes -----------------------------------------------------------

    def add_snapshot(self, snap: Snapshot, joules_cold: float | None, joules_hot: float | None) -> None:
        for name, p in snap.probes.items():
            if p.celsius is not None:  # missing readings are not written
                self._readings.append((snap.ts, name, p.celsius))
        if snap.p_cold is not None:
            self._power.append((snap.ts, "cold", snap.p_cold, joules_cold or 0.0))
        if snap.p_hot is not None:
            self._power.append((snap.ts, "hot", snap.p_hot, joules_hot or 0.0))

    def add_relays(self, ts: float, outputs: RelayOutputs, state: State) -> None:
        row = (int(outputs.peltier), int(outputs.cold_pump), int(outputs.hot_pump), state.value)
        if row == self._last_relays:
            return  # only write on change
        self._last_relays = row
        self._relays.append((ts, *row))

    def add_event(self, ev: Event) -> None:
        self._events.append((ev.ts if ev.ts is not None else time.time(), ev.kind, ev.detail))

    def start_session(self, ts: float) -> int:
        cur = self.conn.execute("INSERT INTO sessions (started) VALUES (?)", (ts,))
        self.conn.commit()
        sid = cur.lastrowid
        assert sid is not None
        self.session_id = sid
        return sid

    def end_session(self, ts: float) -> None:
        if self.session_id is None:
            return
        sid = self.session_id
        self.session_id = None
        self.flush()
        self.conn.execute("UPDATE sessions SET ended = ? WHERE id = ?", (ts, sid))
        self.conn.commit()
        self.archive(sid, ts)

    def flush(self) -> None:
        with self.conn:
            if self._readings:
                self.conn.executemany("INSERT INTO readings VALUES (?, ?, ?)", self._readings)
            if self._power:
                self.conn.executemany("INSERT INTO power VALUES (?, ?, ?, ?)", self._power)
            if self._relays:
                self.conn.executemany("INSERT INTO relays VALUES (?, ?, ?, ?, ?)", self._relays)
            if self._events:
                self.conn.executemany("INSERT INTO events VALUES (?, ?, ?)", self._events)
        self._readings.clear()
        self._power.clear()
        self._relays.clear()
        self._events.clear()
        self._last_commit = self._clock()

    def apply_retention(self, now_wall: float | None = None) -> None:
        cutoff = (now_wall if now_wall is not None else time.time()) - self.retention_s
        with self.conn:
            for table in ("readings", "power", "relays", "events"):
                self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM sessions WHERE ended IS NOT NULL AND ended < ?", (cutoff,))

    def tick(self) -> None:
        """Called every control tick: commits and prunes when due."""
        now = self._clock()
        # Events are rare and the API reads them back, so don't sit on them.
        if self._events or now - self._last_commit >= self.commit_s:
            self.flush()
        if now - self._last_retention >= RETENTION_EVERY_S:
            self._last_retention = now
            self.apply_retention()

    # -- archive ----------------------------------------------------------

    def archive_path(self, session_id: int, ts: float) -> Path | None:
        if not self.archive_dir:
            return None
        d = Path(self.archive_dir)
        if not d.is_dir() or not os.access(d, os.W_OK):
            log.warning("archive_dir %s is missing or not writable; skipping archive", d)
            return None
        date = datetime.fromtimestamp(ts, UTC).strftime("%Y%m%d-%H%M%S")
        return d / f"session-{session_id}-{date}.db"

    def archive(self, session_id: int, ts: float, background: bool = True) -> Path | None:
        """One ``VACUUM INTO`` per session, intended for a USB stick.

        Runs on its own connection in a background thread so a slow stick
        can't stall the control loop (and trip the watchdog).
        """
        dest = self.archive_path(session_id, ts)
        if dest is None or self.db_path == ":memory:":
            return None

        def work() -> None:
            try:
                conn = sqlite3.connect(self.db_path)
                try:
                    conn.execute("VACUUM INTO ?", (str(dest),))
                finally:
                    conn.close()
                log.info("archived session %d to %s", session_id, dest)
            except Exception:
                log.exception("archiving session %d failed", session_id)

        if background:
            th = threading.Thread(target=work, name="archive", daemon=True)
            th.start()
            self._archive_threads.append(th)
        else:
            work()
        return dest

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        finally:
            for th in self._archive_threads:
                th.join(timeout=30)
            self.conn.close()


def recent_events(db_path: str, limit: int = 50) -> list[dict]:
    """Read-only query for the API, on a fresh connection."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [{"ts": ts, "kind": kind, "detail": detail} for ts, kind, detail in rows]
