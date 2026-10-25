"""The whole service (minus HTTP) against FakeBus + the simulator, on a fake clock."""

import pytest

from heatsteal import service
from heatsteal.model import Command, State

from .conftest import make_config


class Clock:
    def __init__(self):
        self.t = 1000.0

    def mono(self):
        return self.t

    def wall(self):
        return 1_700_000_000.0 + self.t


class Harness:
    def __init__(self, tmp_path, **sim):
        self.clock = Clock()
        self.pings = []
        self.cfg = make_config(storage={"db_path": str(tmp_path / "h.db")}, sim={"noise": False, **sim})
        self.loop = service.build(self.cfg, mono=self.clock.mono, wall=self.clock.wall,
                                  notify=self.pings.append, sim_seed=1)
        self.trace = []

    def tick(self, n=1):
        for _ in range(n):
            self.clock.t += self.cfg.timing.sample_s
            self.loop.tick()
            s = self.loop.status()
            self.trace.append((self.clock.t, s["state"], s["probes"]["hot_flow"]["celsius"]))

    def run_until(self, state, limit_s):
        start = self.clock.t
        while self.loop.ctx.state is not state:
            if self.clock.t - start > limit_s:
                raise AssertionError(f"not {state} after {limit_s}s; trace tail {self.trace[-5:]}")
            self.tick()
        return self.clock.t - start

    @property
    def relays(self):
        return self.loop.status()["relays"]

    def close(self):
        self.loop.shutdown()


@pytest.fixture
def harness(tmp_path):
    made = []

    def make(**sim):
        h = Harness(tmp_path, **sim)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def test_normal_session_steals_heat(harness):
    h = harness()
    h.tick(3)
    assert h.loop.handle_command(Command("start")).ok
    h.run_until(State.RUNNING, 15)
    h.tick(300)
    s = h.loop.status()
    assert s["state"] == "RUNNING"
    assert s["relays"] == {"peltier": True, "cold_pump": True, "hot_pump": True}
    assert 10 < s["p_cold_w"] < 30
    assert 10 < s["p_hot_w"] < 35
    assert s["kj_cold"] > 3  # ~20 W for ~5 min
    assert not s["warnings"]
    assert h.loop.handle_command(Command("stop")).ok
    h.run_until(State.IDLE, 65)
    assert h.relays == {"peltier": False, "cold_pump": False, "hot_pump": False}
    assert len(h.pings) >= 300 and set(h.pings) == {"WATCHDOG=1"}


def test_runaway_hot_side_faults(harness):
    h = harness(fault="runaway_hot", fault_after_s=180)
    h.tick(3)
    h.loop.handle_command(Command("start"))
    h.run_until(State.RUNNING, 15)
    h.tick(170)
    assert h.loop.ctx.state is State.RUNNING
    elapsed = h.run_until(State.FAULT, 15 * 60)
    s = h.loop.status()
    assert "hot_flow" in s["last_fault"]
    # Pumps keep running in FAULT to protect the module; Peltier off.
    assert s["relays"] == {"peltier": False, "cold_pump": True, "hot_pump": True}
    # Fault lands within ~10 s of hot_flow first reaching the limit (rather
    # than relying on the 45 °C cutout).
    over = [t for t, _, hot in h.trace if hot is not None and hot >= h.cfg.limits.hot_max_c]
    assert over and h.clock.t - over[0] <= 1.0
    assert elapsed < 12 * 60
    # And the latch holds.
    h.tick(120)
    assert h.loop.ctx.state is State.FAULT


def test_stale_probe_faults(harness):
    h = harness(fault="stale_probe", fault_probe="cold_return", fault_after_s=30)
    h.tick(3)
    h.loop.handle_command(Command("start"))
    h.run_until(State.RUNNING, 15)
    elapsed = h.run_until(State.FAULT, 60)
    assert "cold_return" in h.loop.status()["last_fault"]
    # 30 s to the fault being injected, then sensor_stale_s (5 s) without data.
    assert 30 <= elapsed <= 40


def test_dead_hot_pump_is_caught(harness):
    h = harness(fault="dead_pump", fault_loop="hot", fault_after_s=200)
    h.tick(3)
    h.loop.handle_command(Command("start"))
    h.run_until(State.RUNNING, 15)
    h.tick(195)
    assert not h.loop.status()["warnings"]
    # In the sim a stalled hot loop overheats the hot block, the 45 °C cutout
    # opens, and the cold loop stops moving heat.
    for _ in range(300):
        h.tick()
        s = h.loop.status()
        if any("cutout" in w for w in s["warnings"]):
            break
    else:
        raise AssertionError(f"no cutout warning; trace tail {h.trace[-5:]}")
    assert h.loop.sampler.bus.sim.cutout_open


def test_crash_drops_relays(harness):
    h = harness()
    h.tick(3)
    h.loop.handle_command(Command("start"))
    h.run_until(State.RUNNING, 15)
    assert h.relays["peltier"]

    def boom():
        raise RuntimeError("sensor driver exploded")

    h.loop.sampler.sample = boom
    crashed = []
    h.loop.on_crash = lambda: crashed.append(True)
    h.loop._stop.clear()
    h.loop.run()  # returns after the crash
    assert h.loop.crashed and crashed
    assert h.loop._relays.state().peltier is False
    assert h.loop._relays.state().cold_pump is False


def test_shutdown_turns_everything_off(harness):
    h = harness()
    h.tick(3)
    h.loop.handle_command(Command("start"))
    h.run_until(State.RUNNING, 15)
    h.loop.shutdown()
    assert h.loop._relays.state() == h.loop._relays.state().__class__()
    # Further ticks/commands cannot re-energise anything.
    h.loop.tick()
    assert not h.loop._relays.state().peltier
    assert not h.loop.handle_command(Command("start")).ok
