import time

import pytest
from fastapi.testclient import TestClient

from heatsteal import service
from heatsteal.web import create_app

from .conftest import make_config


@pytest.fixture
def make_client(tmp_path):
    loops = []

    def make(**web):
        cfg = make_config(storage={"db_path": str(tmp_path / "h.db")}, web=web, sim={"noise": False},
                          timing={"sample_s": 0.05, "sensor_stale_s": 5})
        loop = service.build(cfg, notify=lambda m: True)
        loop.start()
        loops.append(loop)
        deadline = time.monotonic() + 5
        while loop.snapshot is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return TestClient(create_app(cfg, loop))

    yield make
    for loop in loops:
        loop.shutdown()


def test_state(make_client):
    c = make_client()
    s = c.get("/api/state").json()
    assert s["state"] == "IDLE"
    assert set(s["probes"]) == {"cold_flow", "cold_return", "hot_flow", "hot_return"}
    assert s["relays"] == {"peltier": False, "cold_pump": False, "hot_pump": False}
    for key in ("p_cold_w", "p_hot_w", "kj_cold", "kj_hot", "last_fault", "warnings", "time_in_state_s"):
        assert key in s


def test_index_served(make_client):
    r = make_client().get("/")
    assert r.status_code == 200 and "Watts" not in r.text[:0] and "/api/state" in r.text


def test_commands(make_client):
    c = make_client()
    r = c.post("/api/command", json={"action": "start"})
    assert r.status_code == 200 and r.json() == {"ok": True, "state": "PRIMING"}
    r = c.post("/api/command", json={"action": "reset"})
    assert r.status_code == 409
    assert r.json()["ok"] is False and "FAULT" in r.json()["reason"]
    r = c.post("/api/command", json={"action": "stop"})
    assert r.json()["state"] == "COOLDOWN"


def test_pump_command(make_client):
    c = make_client()
    r = c.post("/api/command", json={"action": "pump", "loop": "cold", "seconds": 60})
    assert r.status_code == 200
    assert c.get("/api/state").json()["pump_job"]["loop"] == "cold"
    r = c.post("/api/command", json={"action": "pump", "loop": "cold"})
    assert r.status_code == 409


def test_bad_body(make_client):
    c = make_client()
    assert c.post("/api/command", json={"action": "explode"}).status_code == 422


def test_events(make_client):
    c = make_client()
    c.post("/api/command", json={"action": "start"})
    ev = c.get("/api/events?limit=50").json()
    assert any(e["detail"] == "start accepted" for e in ev)


def test_auth(make_client):
    c = make_client(control_token="s3cret")
    assert c.get("/api/state").status_code == 200  # GET stays open
    assert c.post("/api/command", json={"action": "start"}).status_code == 401
    assert c.post("/api/command", json={"action": "start"},
                  headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/api/command", json={"action": "start"}, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert c.get("/api/ui").json()["auth_required"] is True
