"""The provisioned dashboard's SQL must run against our real schema.

Macros are expanded the way the frser-sqlite-datasource README describes:
$__from/$__to are epoch milliseconds, $__unixEpochGroupSeconds(col, n)
buckets a unix-epoch column into n-second groups.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest

from heatsteal.model import Event, RelayOutputs, State
from heatsteal.storage import Storage

from .conftest import make_snap

DEPLOY = Path(__file__).parent.parent / "deploy" / "grafana"
DASH = json.loads((DEPLOY / "dashboards" / "heatsteal.json").read_text())
T0 = 1_700_000_000.0


def expand(sql, frm_ms, to_ms):
    sql = re.sub(r"\$__unixEpochGroupSeconds\((\w+),\s*(\d+)\)", r"cast((\1 / \2) as int) * \2", sql)
    return sql.replace("$__from", str(frm_ms)).replace("$__to", str(to_ms))


def all_targets():
    for p in DASH["panels"]:
        for t in p["targets"]:
            yield p["title"], t
    for a in DASH["annotations"]["list"]:
        if "target" in a:
            yield a["name"], a["target"]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "h.db")
    s = Storage(path)
    s.start_session(T0)
    for i in range(30):
        snap = make_snap(float(i), ts=T0 + i)
        snap = snap.__class__(ts=snap.ts, mono=snap.mono, probes=snap.probes, p_cold=20.0, p_hot=24.0)
        s.add_snapshot(snap, 20.0 * i, 24.0 * i)
    s.add_relays(T0, RelayOutputs(False, True, True), State.PRIMING)
    s.add_event(Event("state", "IDLE -> PRIMING: start", T0))
    s.add_event(Event("command", "start accepted", T0))
    s.flush()
    yield path
    s.close()


@pytest.mark.parametrize("title, target", list(all_targets()), ids=[t for t, _ in all_targets()])
def test_queries_run(db, title, target):
    assert target["datasource"]["uid"] == "heatsteal-sqlite"
    assert target["queryText"] == target["rawQueryText"]
    sql = expand(target["queryText"], int(T0 * 1000) - 60_000, int(T0 * 1000) + 60_000)
    conn = sqlite3.connect(db)
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    assert rows, title
    for tc in target["timeColumns"]:
        assert tc in cols
    if target["queryType"] == "time series":
        # LongToWide needs ascending time
        times = [r[cols.index("time")] for r in rows]
        assert times == sorted(times)


def test_kj_stat(db):
    t = DASH["panels"][2]["targets"][0]
    conn = sqlite3.connect(db)
    ((ts, kj),) = conn.execute(t["queryText"]).fetchall()
    conn.close()
    assert kj == pytest.approx(20.0 * 29 / 1000)


def test_panel_ids_match_page():
    page = (Path(__file__).parent.parent / "src/heatsteal/static/index.html").read_text()
    ids = re.search(r"const panels = \[([\d, ]+)\]", page).group(1)
    assert sorted(int(i) for i in ids.split(",")) == sorted(p["id"] for p in DASH["panels"])
    assert "d-solo/heatsteal/" in page and DASH["uid"] == "heatsteal"


def test_datasource_points_at_service_db():
    from heatsteal.config import Storage as StorageCfg

    text = (DEPLOY / "provisioning" / "datasources" / "heatsteal.yaml").read_text()
    assert f"path: {StorageCfg().db_path}" in text
    assert "uid: heatsteal-sqlite" in text
