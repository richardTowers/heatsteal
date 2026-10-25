import pytest

from heatsteal import controller
from heatsteal.controller import step
from heatsteal.model import Command, RelayOutputs, State

from .conftest import NORMAL, make_config, make_snap

OFF = RelayOutputs()
PUMPS = RelayOutputs(False, True, True)
ALL_ON = RelayOutputs(True, True, True)

START, STOP, RESET = Command("start"), Command("stop"), Command("reset")


class Rig:
    """Drives the pure controller with an injected clock, one tick at a time."""

    def __init__(self, cfg=None, temps=None):
        self.cfg = cfg or make_config()
        self.now = 0.0
        self.temps = dict(NORMAL if temps is None else temps)
        self.stale: set[str] = set()
        self.ctx = controller.initial(self.now)
        self.events = []
        self.outputs = OFF

    def snap(self):
        return make_snap(self.now, self.temps, self.stale)

    def tick(self, command=None):
        r = step(self.cfg, self.ctx, self.snap(), command, self.now)
        self.ctx, self.outputs = r.ctx, r.outputs
        self.events += r.events
        return r

    def run(self, seconds, dt=1.0, each=None):
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now += dt
            if each:
                each(self)
            self.tick()

    def cmd(self, command):
        return self.tick(command).reply

    @property
    def state(self):
        return self.ctx.state

    def to_running(self):
        assert self.cmd(START).ok
        self.run(self.cfg.timing.prime_s)
        assert self.state is State.RUNNING

    def to_fault(self):
        self.to_running()
        self.temps["hot_flow"] = 42.0
        self.tick()
        assert self.state is State.FAULT
        self.temps = dict(NORMAL)

    def kinds(self, kind):
        return [e.detail for e in self.events if e.kind == kind]


# -- outputs per state ---------------------------------------------------------


def test_starts_idle_all_off():
    rig = Rig()
    rig.tick()
    assert rig.state is State.IDLE
    assert rig.outputs == OFF


@pytest.mark.parametrize("state, expected", [
    (State.IDLE, OFF),
    (State.PRIMING, PUMPS),
    (State.RUNNING, ALL_ON),
    (State.COOLDOWN, PUMPS),
    (State.FAULT, PUMPS),
])
def test_outputs_for_each_state(state, expected):
    assert controller.outputs_for(controller.Ctx(state=state)) == expected


def test_peltier_only_on_in_running():
    for s in State:
        out = controller.outputs_for(controller.Ctx(state=s))
        assert out.peltier == (s is State.RUNNING)
        if out.peltier:
            assert out.cold_pump and out.hot_pump


# -- transitions ---------------------------------------------------------------


def test_full_session():
    rig = Rig()
    r = rig.cmd(START)
    assert r.ok and r.state is State.PRIMING
    assert rig.outputs == PUMPS
    rig.run(9)
    assert rig.state is State.PRIMING  # prime_s = 10
    rig.run(1)
    assert rig.state is State.RUNNING
    assert rig.outputs == ALL_ON
    r = rig.cmd(STOP)
    assert r.ok and r.state is State.COOLDOWN
    assert rig.outputs == PUMPS
    rig.run(59)
    assert rig.state is State.COOLDOWN
    rig.run(1)
    assert rig.state is State.IDLE
    assert rig.outputs == OFF
    assert rig.ctx.session_start is None
    assert rig.kinds("state") == [
        "IDLE -> PRIMING: start",
        "PRIMING -> RUNNING: primed",
        "RUNNING -> COOLDOWN: stop",
        "COOLDOWN -> IDLE: cooldown complete",
    ]


def test_stop_from_priming():
    rig = Rig()
    rig.cmd(START)
    rig.run(3)
    assert rig.cmd(STOP).ok
    assert rig.state is State.COOLDOWN


def test_priming_waits_for_fresh_probes():
    rig = Rig()
    rig.cmd(START)
    # A probe that has never read but isn't yet stale: PRIMING must not advance.
    rig.temps["hot_return"] = None
    rig.run(15)
    assert rig.state is State.PRIMING
    rig.temps["hot_return"] = 31.5
    rig.tick()
    assert rig.state is State.RUNNING


def test_session_start_recorded():
    rig = Rig()
    rig.now = 5.0
    rig.cmd(START)
    assert rig.ctx.session_start == 5.0


# -- rejected commands ---------------------------------------------------------


@pytest.mark.parametrize("state, command", [
    (State.PRIMING, START),
    (State.RUNNING, START),
    (State.COOLDOWN, START),
    (State.FAULT, START),
    (State.IDLE, STOP),
    (State.COOLDOWN, STOP),
    (State.FAULT, STOP),
    (State.IDLE, RESET),
    (State.PRIMING, RESET),
    (State.RUNNING, RESET),
    (State.COOLDOWN, RESET),
    (State.PRIMING, Command("pump", "cold", 30)),
    (State.RUNNING, Command("pump", "cold", 30)),
    (State.COOLDOWN, Command("pump", "cold", 30)),
    (State.FAULT, Command("pump", "cold", 30)),
])
def test_invalid_commands_rejected_with_reason(state, command):
    rig = Rig()
    if state is State.PRIMING:
        rig.cmd(START)
    elif state is State.RUNNING:
        rig.to_running()
    elif state is State.COOLDOWN:
        rig.to_running()
        rig.cmd(STOP)
    elif state is State.FAULT:
        rig.to_fault()
    assert rig.state is state
    before = rig.ctx
    r = rig.cmd(command)
    assert not r.ok
    assert r.reason
    assert r.state is state
    assert rig.ctx.state is before.state
    assert any("rejected" in d for d in rig.kinds("command"))


def test_unknown_action_rejected():
    r = Rig().cmd(Command("explode"))
    assert not r.ok and "unknown" in r.reason


def test_start_rejected_when_probe_stale():
    rig = Rig()
    rig.stale = {"cold_return"}
    r = rig.cmd(START)
    assert not r.ok and "cold_return" in r.reason
    assert rig.state is State.IDLE


def test_start_rejected_without_snapshot(cfg):
    ctx = controller.initial(0.0)
    r = step(cfg, ctx, None, START, 0.0)
    assert not r.reply.ok
    assert r.ctx.state is State.IDLE


def test_reset_only_from_fault():
    rig = Rig()
    rig.to_fault()
    r = rig.cmd(RESET)
    assert r.ok and r.state is State.COOLDOWN
    assert rig.outputs == PUMPS
    rig.run(60)
    assert rig.state is State.IDLE
    assert rig.ctx.last_fault  # still reported after reset


def test_reset_with_fault_still_present_refaults():
    rig = Rig()
    rig.to_fault()
    rig.temps["hot_flow"] = 42.0
    r = rig.cmd(RESET)
    assert r.ok
    assert r.state is State.FAULT
    assert rig.state is State.FAULT


# -- pump jobs -----------------------------------------------------------------


def test_pump_job_runs_one_pump_for_fixed_time():
    rig = Rig()
    r = rig.cmd(Command("pump", "cold", 60))
    assert r.ok and r.state is State.IDLE
    assert rig.outputs == RelayOutputs(False, True, False)
    rig.run(59)
    assert rig.outputs == RelayOutputs(False, True, False)
    rig.run(1)
    assert rig.outputs == OFF
    assert rig.ctx.pump_job is None


def test_pump_job_hot():
    rig = Rig()
    rig.cmd(Command("pump", "hot", 5))
    assert rig.outputs == RelayOutputs(False, False, True)


def test_pump_job_can_be_stopped():
    rig = Rig()
    rig.cmd(Command("pump", "hot", 60))
    assert rig.cmd(STOP).ok
    assert rig.outputs == OFF


@pytest.mark.parametrize("command", [
    Command("pump", "warm", 10),
    Command("pump", "cold", 0),
    Command("pump", "cold", -5),
    Command("pump", "cold", 10_000),
    Command("pump", "cold", None),
])
def test_bad_pump_commands(command):
    rig = Rig()
    r = rig.cmd(command)
    assert not r.ok and r.reason
    assert rig.outputs == OFF


def test_no_start_or_second_job_during_pump_job():
    rig = Rig()
    rig.cmd(Command("pump", "cold", 60))
    assert not rig.cmd(START).ok
    assert not rig.cmd(Command("pump", "hot", 10)).ok
    assert rig.outputs == RelayOutputs(False, True, False)


def test_valid_commands():
    rig = Rig()
    assert controller.valid_commands(rig.ctx) == ["start", "pump"]
    rig.to_running()
    assert controller.valid_commands(rig.ctx) == ["stop"]
    rig.cmd(STOP)
    assert controller.valid_commands(rig.ctx) == []


# -- faults --------------------------------------------------------------------


def assert_fault(rig, fragment):
    assert rig.state is State.FAULT
    assert rig.outputs == PUMPS, "pumps must stay on in FAULT"
    assert fragment in rig.ctx.last_fault
    assert any(fragment in d for d in rig.kinds("fault"))


@pytest.mark.parametrize("where", ["PRIMING", "RUNNING", "COOLDOWN"])
def test_stale_probe_faults(where):
    rig = Rig()
    rig.cmd(START)
    if where != "PRIMING":
        rig.run(10)
    if where == "COOLDOWN":
        rig.cmd(STOP)
    assert rig.state.value == where
    rig.stale = {"hot_return"}
    rig.tick()
    assert_fault(rig, "hot_return")


def test_stale_probe_in_idle_does_not_fault():
    rig = Rig()
    rig.stale = {"hot_return"}
    rig.run(10)
    assert rig.state is State.IDLE


def test_sampler_stopped_faults(cfg):
    rig = Rig(cfg)
    rig.to_running()
    old = rig.snap()
    rig.now += 6  # > sensor_stale_s with no new snapshot
    r = step(cfg, rig.ctx, old, None, rig.now)
    assert r.ctx.state is State.FAULT
    assert "sampler" in r.ctx.last_fault


def test_hot_over_limit_faults():
    rig = Rig()
    rig.to_running()
    rig.temps["hot_flow"] = 40.9
    rig.tick()
    assert rig.state is State.RUNNING
    rig.temps["hot_flow"] = 41.0
    rig.tick()
    assert_fault(rig, "hot_flow")


def test_hot_over_limit_in_priming_faults():
    rig = Rig()
    rig.cmd(START)
    rig.temps["hot_flow"] = 41.5
    rig.tick()
    assert_fault(rig, "hot_flow")


def test_cold_under_limit_faults():
    rig = Rig()
    rig.to_running()
    rig.temps["cold_flow"] = 5.0
    rig.tick()
    assert_fault(rig, "cold_flow")


def test_fault_is_latched():
    rig = Rig()
    rig.to_fault()
    rig.run(600)  # conditions are back to normal
    assert rig.state is State.FAULT
    assert rig.outputs == PUMPS


def test_cooldown_keeps_pumps_on_and_peltier_off():
    rig = Rig()
    rig.to_running()
    rig.cmd(STOP)
    for _ in range(59):
        rig.run(1)
        assert rig.outputs == PUMPS


def test_max_run_minutes_ends_in_cooldown():
    rig = Rig(make_config(timing={"max_run_minutes": 5}))
    rig.to_running()
    rig.run(5 * 60 - 11)
    assert rig.state is State.RUNNING
    rig.run(1)
    assert rig.state is State.COOLDOWN
    assert rig.outputs == PUMPS
    assert not rig.kinds("fault")
    assert any("max_run_minutes" in d for d in rig.kinds("info"))


# -- dead-pump heuristics ------------------------------------------------------


def ramp(probe, k_per_min):
    def each(rig):
        rig.temps[probe] += k_per_min / 60.0
    return each


@pytest.mark.parametrize("action", ["warn", "fault"])
def test_hot_rise_heuristic(action):
    rig = Rig(make_config(limits={"dead_pump_action": action, "hot_max_c": 44.0}))
    rig.to_running()
    rig.run(120)  # warm-up, steady
    rig.run(60, each=ramp("hot_flow", 4.0))
    if action == "fault":
        assert_fault(rig, "hot_flow rising")
    else:
        assert rig.state is State.RUNNING
        assert any("hot_flow rising" in w for w in rig.ctx.warning_messages())
        assert len([d for d in rig.kinds("warn") if "hot_flow rising" in d]) == 1  # once, not every tick


def test_heuristics_inactive_during_warmup():
    rig = Rig(make_config(limits={"dead_pump_action": "fault", "hot_max_c": 44.0}))
    rig.to_running()
    rig.temps["hot_return"] = rig.temps["hot_flow"] = 25.0
    rig.run(100, each=ramp("hot_flow", 6.0))  # 10 K rise, all within warmup_s
    assert rig.state is State.RUNNING


def test_cold_fall_heuristic():
    rig = Rig(make_config(limits={"cold_fall_action": "fault"}))
    rig.to_running()
    rig.run(120)
    rig.run(60, each=ramp("cold_flow", -4.0))
    assert_fault(rig, "cold_flow falling")


def test_loop_dt_heuristic_and_clear():
    rig = Rig()
    rig.to_running()
    rig.run(120)
    rig.temps["cold_flow"], rig.temps["cold_return"] = 10.0, 23.0  # 13 K
    rig.tick()
    assert rig.state is State.RUNNING
    assert any("cold loop ΔT" in w for w in rig.ctx.warning_messages())
    rig.temps = dict(NORMAL)
    rig.tick()
    assert rig.ctx.warnings == ()
    assert any("cleared" in d for d in rig.kinds("info"))


def test_loop_dt_fault_mode():
    rig = Rig(make_config(limits={"loop_dt_action": "fault"}))
    rig.to_running()
    rig.run(120)
    rig.temps["hot_flow"], rig.temps["hot_return"] = 40.0, 27.0
    rig.tick()
    assert_fault(rig, "hot loop ΔT")


def test_warnings_cleared_on_leaving_running():
    rig = Rig()
    rig.to_running()
    rig.run(120)
    rig.temps["cold_flow"], rig.temps["cold_return"] = 10.0, 23.0
    rig.tick()
    assert rig.ctx.warnings
    rig.cmd(STOP)
    assert rig.ctx.warnings == ()


def test_step_is_pure(cfg):
    ctx = controller.initial(0.0)
    snap = make_snap(0.0)
    a = step(cfg, ctx, snap, START, 0.0)
    b = step(cfg, ctx, snap, START, 0.0)
    assert a.ctx == b.ctx and a.outputs == b.outputs
    assert ctx.state is State.IDLE  # input untouched


def test_slow_genuine_warmup_does_not_warn():
    rig = Rig(make_config(limits={"dead_pump_action": "fault"}))
    rig.to_running()
    rig.run(120, each=ramp("hot_flow", 1.5))  # 36 -> 39 °C, within hot_max_c
    assert rig.state is State.RUNNING
    assert rig.ctx.warnings == ()


def test_cutout_warning_when_no_heat_moving():
    rig = Rig()
    rig.to_running()
    rig.run(120)
    # Cutout open: Peltier commanded on but the cold loop's ΔT collapses.
    rig.temps["cold_flow"] = rig.temps["cold_return"] = 25.0
    rig.run(30)
    assert rig.ctx.warnings == ()
    rig.run(1)
    assert rig.state is State.RUNNING  # never a fault: the cutout is a safe state
    assert rig.outputs == ALL_ON
    assert any("cutout" in w for w in rig.ctx.warning_messages())
    rig.temps = dict(NORMAL)
    rig.tick()
    assert not any("cutout" in w for w in rig.ctx.warning_messages())
    # The hold timer restarts from scratch.
    rig.temps["hot_flow"] = rig.temps["hot_return"] = 33.0
    rig.run(30)
    assert not any("cutout" in w for w in rig.ctx.warning_messages())
    rig.run(1)
    assert any("hot loop" in w and "cutout" in w for w in rig.ctx.warning_messages())
