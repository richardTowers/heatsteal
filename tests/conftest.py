from __future__ import annotations

from pathlib import Path

import pytest

from heatsteal import config as config_mod
from heatsteal.config import PROBES
from heatsteal.model import ProbeReading, Snapshot

FIXTURES = Path(__file__).parent / "fixtures"

ADDRESSES = {p: f"28-00000000000{i + 1}" for i, p in enumerate(PROBES)}


def make_config(**sections):
    """A valid config dict with overrides, e.g. make_config(timing={"prime_s": 5})."""
    raw = {"hardware": "fake", "sensors": dict(ADDRESSES)}
    raw.update(sections)
    return config_mod.from_dict(raw)


@pytest.fixture
def cfg():
    return make_config()


# Comfortable mid-session temperatures: ΔT of 4.5 K on each loop.
NORMAL = {"cold_flow": 20.0, "cold_return": 24.5, "hot_flow": 36.0, "hot_return": 31.5}


def make_snap(now: float, temps: dict | None = None, stale: set[str] = frozenset(), ts: float | None = None):
    temps = dict(NORMAL if temps is None else temps)
    probes = {}
    for name in PROBES:
        v = temps.get(name)
        probes[name] = ProbeReading(
            name=name,
            address=ADDRESSES[name],
            celsius=None if name in stale else v,
            last_good_c=v,
            last_good_mono=now - (10.0 if name in stale else 0.0),
            stale=name in stale,
        )
    return Snapshot(ts=1_700_000_000.0 + now if ts is None else ts, mono=now, probes=probes, p_cold=None, p_hot=None)
