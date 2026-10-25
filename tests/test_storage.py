import sqlite3

from heatsteal.model import Event, RelayOutputs, State
from heatsteal.storage import Storage, recent_events

from .conftest import make_snap


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_schema_and_pragmas(tmp_path):
    db = str(tmp_path / "h.db")
    s = Storage(db)
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s.conn.execute("PRAGMA synchronous").fetchone()[0] == 0
    tables = {r[0] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"sessions", "readings", "power", "relays", "events"}
    s.close()


def test_batches_commit_every_5s(tmp_path):
    db = str(tmp_path / "h.db")
    clock = Clock()
    s = Storage(db, clock=clock)
    snap = make_snap(0.0, stale={"hot_return"})
    snap = snap.__class__(ts=snap.ts, mono=snap.mono, probes=snap.probes, p_cold=20.0, p_hot=None)
    s.add_snapshot(snap, 100.0, 0.0)
    s.tick()
    assert rows(db, "SELECT count(*) FROM readings") == [(0,)]  # not yet committed
    clock.t = 5.0
    s.tick()
    readings = rows(db, "SELECT sensor FROM readings ORDER BY sensor")
    assert [r[0] for r in readings] == ["cold_flow", "cold_return", "hot_flow"]  # missing not written
    assert rows(db, "SELECT loop, watts, joules FROM power") == [("cold", 20.0, 100.0)]
    s.close()


def test_relays_written_only_on_change(tmp_path):
    db = str(tmp_path / "h.db")
    s = Storage(db)
    s.add_relays(1.0, RelayOutputs(), State.IDLE)
    s.add_relays(2.0, RelayOutputs(), State.IDLE)
    s.add_relays(3.0, RelayOutputs(False, True, True), State.PRIMING)
    s.add_relays(4.0, RelayOutputs(False, True, True), State.PRIMING)
    s.add_relays(5.0, RelayOutputs(False, True, True), State.FAULT)  # state changed
    s.flush()
    assert rows(db, "SELECT ts, peltier, cold_pump, hot_pump, state FROM relays") == [
        (1.0, 0, 0, 0, "IDLE"), (3.0, 0, 1, 1, "PRIMING"), (5.0, 0, 1, 1, "FAULT")]
    s.close()


def test_events_committed_promptly_and_readable(tmp_path):
    db = str(tmp_path / "h.db")
    s = Storage(db, clock=Clock())
    s.add_event(Event("fault", "hot_flow too hot", 10.0))
    s.add_event(Event("info", "later", 11.0))
    s.tick()
    ev = recent_events(db, 1)
    assert ev == [{"ts": 11.0, "kind": "info", "detail": "later"}]
    s.close()


def test_retention(tmp_path):
    db = str(tmp_path / "h.db")
    s = Storage(db, retention_hours=1)
    now = 100_000.0
    s.add_event(Event("info", "old", now - 3601))
    s.add_event(Event("info", "new", now - 10))
    s.add_relays(now - 7200, RelayOutputs(), State.IDLE)
    s.flush()
    s.apply_retention(now)
    assert rows(db, "SELECT detail FROM events") == [("new",)]
    assert rows(db, "SELECT count(*) FROM relays") == [(0,)]
    s.close()


def test_retention_runs_once_a_minute(tmp_path, monkeypatch):
    clock = Clock()
    s = Storage(str(tmp_path / "h.db"), clock=clock)
    calls = []
    monkeypatch.setattr(s, "apply_retention", lambda *a: calls.append(clock.t))
    for t in range(0, 181):
        clock.t = float(t)
        s.tick()
    assert calls == [60.0, 120.0, 180.0]
    s.close()


def test_sessions_and_archive(tmp_path):
    db = str(tmp_path / "h.db")
    archive = tmp_path / "usb"
    archive.mkdir()
    s = Storage(db, archive_dir=str(archive))
    sid = s.start_session(1_700_000_000.0)
    s.add_event(Event("info", "during", 1_700_000_001.0))
    s.end_session(1_700_000_100.0)
    s.close()  # joins the archive thread
    assert rows(db, "SELECT id, started, ended FROM sessions") == [(sid, 1_700_000_000.0, 1_700_000_100.0)]
    files = list(archive.glob(f"session-{sid}-*.db"))
    assert len(files) == 1
    assert rows(str(files[0]), "SELECT detail FROM events") == [("during",)]


def test_archive_skipped_when_dir_missing(tmp_path):
    s = Storage(str(tmp_path / "h.db"), archive_dir=str(tmp_path / "not-plugged-in"))
    sid = s.start_session(1.0)
    s.end_session(2.0)
    assert s.archive(sid, 2.0) is None
    s.close()
