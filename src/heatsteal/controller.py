"""The safety state machine. Pure logic: no I/O, no clock of its own.

``step(cfg, ctx, snapshot, command, now)`` returns the new context, the relay
outputs to apply, any events, and the reply to the command (if there was
one). ``now`` is monotonic seconds; tests pass whatever they like.

States and outputs:

    IDLE      all off (unless a timed ``pump`` job is running one pump)
    PRIMING   pumps on, Peltier off; at least prime_s, needs all probes fresh
    RUNNING   everything on
    COOLDOWN  pumps on, Peltier off; cooldown_s then IDLE
    FAULT     pumps on, Peltier off; latched until ``reset``

Pumps stay on in COOLDOWN and FAULT deliberately: stopping the flow while
the two faces of the Peltier are at different temperatures cracks modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .config import LOOPS, Config
from .model import Command, CommandResult, Event, RelayOutputs, Snapshot, State

PUMP_JOB_MAX_S = 600.0
RATE_WINDOW_S = 60.0
RATE_MIN_SPAN_S = 30.0

ACTIVE_STATES = (State.PRIMING, State.RUNNING, State.COOLDOWN)


@dataclass(frozen=True)
class PumpJob:
    loop: str
    until: float


@dataclass(frozen=True)
class Ctx:
    state: State = State.IDLE
    entered: float = 0.0  # when the current state was entered
    session_start: float | None = None  # set from start until back in IDLE
    running_since: float | None = None
    pump_job: PumpJob | None = None
    last_fault: str = ""  # most recent fault reason; survives reset
    warnings: tuple[tuple[str, str], ...] = ()  # (id, message), active only
    low_dt_since: float | None = None  # RUNNING with a loop ΔT below min_loop_dt_k since
    # (t, hot_flow, cold_flow) for rate-of-change heuristics, last RATE_WINDOW_S
    history: tuple[tuple[float, float | None, float | None], ...] = ()

    def time_in_state(self, now: float) -> float:
        return now - self.entered

    def warning_messages(self) -> list[str]:
        return [msg for _, msg in self.warnings]


@dataclass
class StepResult:
    ctx: Ctx
    outputs: RelayOutputs
    events: list[Event] = field(default_factory=list)
    reply: CommandResult | None = None


def initial(now: float) -> Ctx:
    """Always start in IDLE."""
    return Ctx(state=State.IDLE, entered=now)


def outputs_for(ctx: Ctx) -> RelayOutputs:
    if ctx.state is State.RUNNING:
        return RelayOutputs(peltier=True, cold_pump=True, hot_pump=True)
    if ctx.state in (State.PRIMING, State.COOLDOWN, State.FAULT):
        return RelayOutputs(peltier=False, cold_pump=True, hot_pump=True)
    # IDLE
    if ctx.pump_job is not None:
        return RelayOutputs(
            peltier=False,
            cold_pump=ctx.pump_job.loop == "cold",
            hot_pump=ctx.pump_job.loop == "hot",
        )
    return RelayOutputs()


def valid_commands(ctx: Ctx) -> list[str]:
    if ctx.state is State.IDLE:
        return ["stop"] if ctx.pump_job else ["start", "pump"]
    if ctx.state in (State.PRIMING, State.RUNNING):
        return ["stop"]
    if ctx.state is State.FAULT:
        return ["reset"]
    return []


# -- helpers ------------------------------------------------------------------


def _transition(ctx: Ctx, new: State, now: float, why: str, events: list[Event]) -> Ctx:
    if ctx.state is new:
        return ctx
    events.append(Event("state", f"{ctx.state.value} -> {new.value}: {why}"))
    changes: dict = {"state": new, "entered": now}
    if new is State.RUNNING:
        changes["running_since"] = now
    elif ctx.state is State.RUNNING:
        changes["running_since"] = None
    if new is not State.RUNNING:
        changes["warnings"] = ()
        changes["low_dt_since"] = None
    if new is State.IDLE:
        changes["session_start"] = None
    return replace(ctx, **changes)


def _fault(ctx: Ctx, now: float, reason: str, events: list[Event]) -> Ctx:
    events.append(Event("fault", reason))
    ctx = _transition(ctx, State.FAULT, now, reason, events)
    return replace(ctx, last_fault=reason)


def _effective_snapshot_stale(cfg: Config, snap: Snapshot | None, now: float) -> bool:
    """The sampler itself has stopped publishing."""
    return snap is None or now - snap.mono > cfg.timing.sensor_stale_s


def _slope_k_per_min(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 5 or points[-1][0] - points[0][0] < RATE_MIN_SPAN_S:
        return None
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    if den == 0:
        return None
    num = sum((t - mt) * (v - mv) for t, v in points)
    return num / den * 60.0


def _update_history(ctx: Ctx, snap: Snapshot | None) -> Ctx:
    if snap is None:
        return ctx
    if ctx.history and ctx.history[-1][0] >= snap.mono:
        return ctx  # already have this sample
    row = (snap.mono, snap.value("hot_flow"), snap.value("cold_flow"))
    cutoff = snap.mono - RATE_WINDOW_S
    hist = tuple(h for h in ctx.history if h[0] >= cutoff) + (row,)
    return replace(ctx, history=hist)


def _loop_dts(snap: Snapshot) -> dict[str, float]:
    """Each loop's ΔT, signed so that positive means the rig is working."""
    out = {}
    pairs = {"cold": ("cold_return", "cold_flow"), "hot": ("hot_flow", "hot_return")}
    for loop, (a, b) in pairs.items():
        va, vb = snap.value(a), snap.value(b)
        if va is not None and vb is not None:
            out[loop] = va - vb
    return out


def _cutout_check(cfg: Config, ctx: Ctx, snap: Snapshot, now: float) -> tuple[Ctx, list[tuple[str, str, str]]]:
    """Peltier commanded on, yet a loop has stopped moving heat.

    The usual cause is the 45 °C bimetallic cutout in series with the
    Peltier having opened (which itself usually means a hot-side flow
    problem). The cutout is a safe state, so this is only ever a warning.
    """
    lim = cfg.limits
    low = [loop for loop, dt in _loop_dts(snap).items() if dt < lim.min_loop_dt_k]
    if not low:
        return replace(ctx, low_dt_since=None), []
    since = ctx.low_dt_since if ctx.low_dt_since is not None else now
    ctx = replace(ctx, low_dt_since=since)
    if now - since < lim.min_loop_dt_hold_s:
        return ctx, []
    msg = (f"Peltier on but no heat moving in {' and '.join(low)} loop for {now - since:.0f} s; "
           "45 °C cutout may have tripped, check hot-side flow")
    return ctx, [("cutout", "warn", msg)]


def _dead_pump_checks(cfg: Config, ctx: Ctx, snap: Snapshot) -> list[tuple[str, str, str]]:
    """Returns (id, action, message) for every heuristic currently tripped."""
    lim = cfg.limits
    tripped = []

    hot_pts = [(t, h) for t, h, _ in ctx.history if h is not None]
    rise = _slope_k_per_min(hot_pts)
    if rise is not None and rise > lim.hot_rise_max_k_per_min:
        tripped.append(
            ("hot_rise", lim.action("hot_rise"),
             f"hot_flow rising {rise:.1f} K/min (limit {lim.hot_rise_max_k_per_min:g}); hot pump dead?")
        )

    cold_pts = [(t, c) for t, _, c in ctx.history if c is not None]
    fall = _slope_k_per_min(cold_pts)
    if fall is not None and -fall > lim.cold_fall_max_k_per_min:
        tripped.append(
            ("cold_fall", lim.action("cold_fall"),
             f"cold_flow falling {-fall:.1f} K/min (limit {lim.cold_fall_max_k_per_min:g}); cold pump dead?")
        )

    for loop, dt in _loop_dts(snap).items():
        if abs(dt) > lim.loop_dt_max_k:
            tripped.append(
                (f"loop_dt_{loop}", lim.action("loop_dt"),
                 f"{loop} loop ΔT {dt:.1f} K (limit {lim.loop_dt_max_k:g}); {loop} pump dead?")
            )
    return tripped


def _evaluate(cfg: Config, ctx: Ctx, snap: Snapshot | None, now: float, events: list[Event]) -> Ctx:
    """Timers and fault checks. Idempotent for a given (snap, now)."""
    t = cfg.timing
    lim = cfg.limits

    if ctx.state is State.IDLE and ctx.pump_job is not None and now >= ctx.pump_job.until:
        events.append(Event("info", f"pump job on {ctx.pump_job.loop} loop finished"))
        ctx = replace(ctx, pump_job=None)

    if ctx.state in ACTIVE_STATES:
        if _effective_snapshot_stale(cfg, snap, now):
            return _fault(ctx, now, "no sensor data: sampler has stopped", events)
        assert snap is not None
        stale = snap.stale_probes()
        if stale:
            return _fault(ctx, now, f"probe(s) stale: {', '.join(stale)}", events)
        hot = snap.value("hot_flow")
        if hot is not None and hot >= lim.hot_max_c:
            return _fault(ctx, now, f"hot_flow {hot:.2f} °C ≥ limit {lim.hot_max_c:g} °C", events)
        cold = snap.value("cold_flow")
        if cold is not None and cold <= lim.cold_min_c:
            return _fault(ctx, now, f"cold_flow {cold:.2f} °C ≤ limit {lim.cold_min_c:g} °C", events)

    if ctx.state in (State.PRIMING, State.RUNNING) and ctx.session_start is not None:
        if now - ctx.session_start >= t.max_run_minutes * 60:
            events.append(Event("info", f"session reached max_run_minutes ({t.max_run_minutes:g})"))
            return _transition(ctx, State.COOLDOWN, now, "max run time reached", events)

    if ctx.state is State.PRIMING:
        assert snap is not None
        if ctx.time_in_state(now) >= t.prime_s and snap.all_fresh():
            return _transition(ctx, State.RUNNING, now, "primed", events)

    elif ctx.state is State.RUNNING:
        assert snap is not None and ctx.running_since is not None
        if now - ctx.running_since >= t.warmup_s:
            ctx, cutout = _cutout_check(cfg, ctx, snap, now)
            tripped = _dead_pump_checks(cfg, ctx, snap) + cutout
            for hid, action, msg in tripped:
                if action == "fault":
                    return _fault(ctx, now, msg, events)
            active = {hid: msg for hid, action, msg in tripped}
            before = dict(ctx.warnings)
            for hid, msg in active.items():
                if hid not in before:
                    events.append(Event("warn", msg))
            for hid in before:
                if hid not in active:
                    events.append(Event("info", f"warning cleared: {before[hid]}"))
            ctx = replace(ctx, warnings=tuple(sorted(active.items())))

    elif ctx.state is State.COOLDOWN:
        if ctx.time_in_state(now) >= t.cooldown_s:
            return _transition(ctx, State.IDLE, now, "cooldown complete", events)

    return ctx


def _command(
    cfg: Config, ctx: Ctx, snap: Snapshot | None, cmd: Command, now: float, events: list[Event]
) -> tuple[Ctx, CommandResult]:
    def reject(reason: str) -> tuple[Ctx, CommandResult]:
        events.append(Event("command", f"{cmd.describe()} rejected: {reason}"))
        return ctx, CommandResult(False, ctx.state, reason)

    def accept(new: Ctx) -> tuple[Ctx, CommandResult]:
        events.append(Event("command", f"{cmd.describe()} accepted"))
        return new, CommandResult(True, new.state)

    s = ctx.state
    if cmd.action == "start":
        if s is not State.IDLE:
            return reject(f"can only start from IDLE (state is {s.value})")
        if ctx.pump_job is not None:
            return reject("a pump job is running; stop it first")
        if _effective_snapshot_stale(cfg, snap, now):
            return reject("no sensor data")
        assert snap is not None
        if not snap.all_fresh():
            missing = [n for n, p in snap.probes.items() if p.stale or p.last_good_c is None]
            return reject(f"probe(s) not reading: {', '.join(missing)}")
        new = _transition(ctx, State.PRIMING, now, "start", events)
        return accept(replace(new, session_start=now))

    if cmd.action == "stop":
        if s in (State.PRIMING, State.RUNNING):
            return accept(_transition(ctx, State.COOLDOWN, now, "stop", events))
        if s is State.IDLE and ctx.pump_job is not None:
            return accept(replace(ctx, pump_job=None))
        return reject(f"nothing to stop in {s.value}")

    if cmd.action == "reset":
        if s is not State.FAULT:
            return reject(f"reset only applies in FAULT (state is {s.value})")
        return accept(_transition(ctx, State.COOLDOWN, now, "reset", events))

    if cmd.action == "pump":
        if s is not State.IDLE:
            return reject(f"pump jobs only run from IDLE (state is {s.value})")
        if ctx.pump_job is not None:
            return reject("a pump job is already running")
        if cmd.loop not in LOOPS:
            return reject(f"loop must be one of {', '.join(LOOPS)}")
        if cmd.seconds is None or not 0 < cmd.seconds <= PUMP_JOB_MAX_S:
            return reject(f"seconds must be between 0 and {PUMP_JOB_MAX_S:g}")
        return accept(replace(ctx, pump_job=PumpJob(cmd.loop, now + cmd.seconds)))

    return reject(f"unknown action {cmd.action!r}")


def step(
    cfg: Config,
    ctx: Ctx,
    snapshot: Snapshot | None,
    command: Command | None,
    now: float,
) -> StepResult:
    events: list[Event] = []
    ctx = _update_history(ctx, snapshot)
    ctx = _evaluate(cfg, ctx, snapshot, now, events)
    reply = None
    if command is not None:
        ctx, reply = _command(cfg, ctx, snapshot, command, now, events)
        ctx = _evaluate(cfg, ctx, snapshot, now, events)
        # e.g. a reset into COOLDOWN that re-faults at once: report where we ended up.
        reply = replace(reply, state=ctx.state)
    return StepResult(ctx=ctx, outputs=outputs_for(ctx), events=events, reply=reply)
