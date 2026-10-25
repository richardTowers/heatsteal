"""The ``heatsteal`` service: wires sensors, controller, relays, storage and web.

One process, one owner of the GPIO. The control thread runs the sampler and
the controller once per ``sample_s`` and is the only code that touches the
relay object. The web server (main thread) only reads the latest status and
puts commands on the control thread's queue.

On any unhandled exception in the control thread: relays all off, log it,
exit non-zero. systemd restarts us and we come back in IDLE.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import queue
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import config as config_mod
from . import controller, physics
from .config import PROBES, Config
from .model import Command, CommandResult, Event, ProbeReading, Snapshot
from .relays import Relays, make_relays
from .sensors import FakeBus, SensorBus, SysfsBus
from .storage import Storage, recent_events

log = logging.getLogger("heatsteal")

COMMAND_TIMEOUT_S = 5.0


# -- systemd notify -----------------------------------------------------------


def sd_notify(message: str) -> bool:
    """Send a message to systemd's notify socket. No-op outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):  # abstract namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError as e:
        log.warning("sd_notify(%r) failed: %s", message, e)
        return False


# -- sampler ------------------------------------------------------------------


class Sampler:
    """Reads every probe, applies offsets, computes power. Publishes Snapshots."""

    def __init__(
        self,
        cfg: Config,
        bus: SensorBus,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.mono = mono
        self.wall = wall
        self.started = mono()
        self._last_good: dict[str, tuple[float, float]] = {}  # probe -> (mono, celsius)

    def sample(self) -> Snapshot:
        addrs = [self.cfg.sensors[p] for p in PROBES]
        raw = self.bus.read(addrs)
        now = self.mono()
        ts = self.wall()
        stale_s = self.cfg.timing.sensor_stale_s
        probes = {}
        for name in PROBES:
            addr = self.cfg.sensors[name]
            r = raw.get(addr)
            celsius = None
            error = "no reading"
            if r is not None and r.celsius is not None:
                celsius = r.celsius + self.cfg.offset(name)
                self._last_good[name] = (now, celsius)
                error = ""
            elif r is not None:
                error = r.error
            good = self._last_good.get(name)
            ref = good[0] if good else self.started
            probes[name] = ProbeReading(
                name=name,
                address=addr,
                celsius=celsius,
                last_good_c=good[1] if good else None,
                last_good_mono=good[0] if good else None,
                stale=now - ref > stale_s,
                error=error,
            )

        f = self.cfg.flow
        cf, cr = probes["cold_flow"].celsius, probes["cold_return"].celsius
        hf, hr = probes["hot_flow"].celsius, probes["hot_return"].celsius
        p_cold = physics.power_cold(f.cold, f.specific_heat, cf, cr) if cf is not None and cr is not None else None
        p_hot = physics.power_hot(f.hot, f.specific_heat, hf, hr) if hf is not None and hr is not None else None
        return Snapshot(ts=ts, mono=now, probes=probes, p_cold=p_cold, p_hot=p_hot)


# -- control loop -------------------------------------------------------------


@dataclass
class _Pending:
    command: Command
    future: concurrent.futures.Future


class ControlLoop:
    """Owns the relays and the controller context. Runs on its own thread."""

    def __init__(
        self,
        cfg: Config,
        sampler: Sampler,
        relays: Relays,
        storage: Storage,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        notify: Callable[[str], bool] = sd_notify,
    ) -> None:
        self.cfg = cfg
        self.sampler = sampler
        self._relays = relays  # nobody else gets a reference to this
        self.storage = storage
        self.mono = mono
        self.wall = wall
        self.notify = notify
        self.ctx = controller.initial(mono())
        self.snapshot: Snapshot | None = None
        self.energy_cold = physics.EnergyIntegrator()
        self.energy_hot = physics.EnergyIntegrator()
        self._lock = threading.RLock()
        self._queue: queue.Queue[_Pending] = queue.Queue()
        self._halted = False
        self._stop = threading.Event()
        self._status: dict = {}
        self._thread: threading.Thread | None = None
        self.crashed = False
        self.on_crash: Callable[[], None] = lambda: None
        self._relays.all_off()
        self._record_event(Event("info", "service started in IDLE"))
        self._publish_status()

    # -- one step ---------------------------------------------------------

    def _record_event(self, ev: Event) -> None:
        if ev.ts is None:
            ev = Event(ev.kind, ev.detail, self.wall())
        level = {"fault": logging.ERROR, "warn": logging.WARNING}.get(ev.kind, logging.INFO)
        log.log(level, "[%s] %s", ev.kind, ev.detail)
        self.storage.add_event(ev)

    def _apply(self, result: controller.StepResult, ts: float) -> None:
        old = self.ctx
        new = result.ctx
        if old.session_start is None and new.session_start is not None:
            sid = self.storage.start_session(ts)
            self.energy_cold.reset()
            self.energy_hot.reset()
            self._record_event(Event("info", f"session {sid} started"))
        self.ctx = new
        for ev in result.events:
            self._record_event(ev)
        self._relays.apply(result.outputs)  # raises on an invariant breach -> crash -> all off
        self.storage.add_relays(ts, self._relays.state(), new.state)
        if old.session_start is not None and new.session_start is None:
            ended = self.storage.session_id
            self.storage.end_session(ts)
            self._record_event(Event("info", f"session {ended} ended: {self.energy_cold.kilojoules:.2f} kJ stolen"))

    def tick(self) -> None:
        """Sample, step the controller, drive relays, store. Once per sample_s."""
        snap = self.sampler.sample()  # may block ~1 s on real hardware; no lock held
        with self._lock:
            if self._halted:
                return
            self.snapshot = snap
            if self.ctx.session_start is not None:
                self.energy_cold.add(snap.mono, snap.p_cold)
                self.energy_hot.add(snap.mono, snap.p_hot)
            result = controller.step(self.cfg, self.ctx, snap, None, self.mono())
            self._apply(result, snap.ts)
            self.storage.add_snapshot(snap, self.energy_cold.joules, self.energy_hot.joules)
            self.storage.tick()
            self._publish_status()
        self.notify("WATCHDOG=1")

    def handle_command(self, cmd: Command) -> CommandResult:
        with self._lock:
            if self._halted:
                return CommandResult(False, self.ctx.state, "service is shutting down")
            result = controller.step(self.cfg, self.ctx, self.snapshot, cmd, self.mono())
            self._apply(result, self.wall())
            self.storage.tick()
            self._publish_status()
            assert result.reply is not None
            return result.reply

    # -- status for the API -----------------------------------------------

    def _publish_status(self) -> None:
        now = self.mono()
        ctx = self.ctx
        snap = self.snapshot
        probes = {}
        for name in PROBES:
            p = snap.probes.get(name) if snap else None
            probes[name] = {
                "celsius": None if p is None else p.last_good_c,
                "stale": True if p is None else p.stale,
                "age_s": None if p is None or p.last_good_mono is None else round(now - p.last_good_mono, 1),
                "address": self.cfg.sensors[name],
                "error": "" if p is None else p.error,
            }
        job = ctx.pump_job
        self._status = {
            "ts": self.wall(),
            "hardware": self.cfg.hardware,
            "state": ctx.state.value,
            "time_in_state_s": round(ctx.time_in_state(now), 1),
            "session_active": ctx.session_start is not None,
            "session_s": None if ctx.session_start is None else round(now - ctx.session_start, 1),
            "probes": probes,
            "relays": self._relays.state().as_dict(),
            "p_cold_w": None if snap is None else snap.p_cold,
            "p_hot_w": None if snap is None else snap.p_hot,
            "kj_cold": self.energy_cold.kilojoules,
            "kj_hot": self.energy_hot.kilojoules,
            "last_fault": ctx.last_fault,
            "warnings": ctx.warning_messages(),
            "valid_commands": controller.valid_commands(ctx),
            "pump_job": None if job is None else {"loop": job.loop, "remaining_s": round(max(0.0, job.until - now), 1)},
        }

    def status(self) -> dict:
        return self._status  # replaced wholesale, never mutated: safe to share

    def recent_events(self, limit: int = 50) -> list[dict]:
        return recent_events(self.storage.db_path, limit)

    # -- threading --------------------------------------------------------

    def submit(self, cmd: Command, timeout: float = COMMAND_TIMEOUT_S) -> CommandResult:
        """Called from the web thread. Blocks until the control thread answers."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put(_Pending(cmd, fut))
        return fut.result(timeout=timeout)

    def run(self) -> None:
        period = self.cfg.timing.sample_s
        next_tick = self.mono()
        try:
            while not self._stop.is_set():
                now = self.mono()
                if now >= next_tick:
                    self.tick()
                    next_tick += period
                    if next_tick < self.mono():  # fell behind: don't burst
                        next_tick = self.mono() + period
                    continue
                try:
                    pending = self._queue.get(timeout=min(next_tick - now, period))
                except queue.Empty:
                    continue
                if pending.future.set_running_or_notify_cancel():
                    pending.future.set_result(self.handle_command(pending.command))
        except BaseException:
            self.crashed = True
            try:
                self._relays.all_off()
            finally:
                log.critical("control loop crashed; relays off", exc_info=True)
                self.on_crash()
        finally:
            self._drain_queue()

    def _drain_queue(self) -> None:
        while True:
            try:
                p = self._queue.get_nowait()
            except queue.Empty:
                return
            if p.future.set_running_or_notify_cancel():
                p.future.set_result(CommandResult(False, self.ctx.state, "service is shutting down"))

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="control", daemon=True)
        self._thread.start()

    def emergency_off(self) -> None:
        """Relays off now and stop driving them. Safe from a signal handler."""
        self._stop.set()
        with self._lock:
            self._halted = True
            self._relays.all_off()

    def shutdown(self) -> None:
        self.emergency_off()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self._lock:
            self._relays.all_off()
            try:
                if self.ctx.session_start is not None:
                    self.storage.end_session(self.wall())
                self._record_event(Event("info", "service stopped; relays off"))
            finally:
                self.storage.close()


# -- construction -------------------------------------------------------------


def build(cfg: Config, mono=time.monotonic, wall=time.time, notify=sd_notify, sim_seed=None) -> ControlLoop:
    relays = make_relays(cfg.hardware, cfg.relays)
    try:
        if cfg.hardware == "pi":
            bus: SensorBus = SysfsBus()
        else:
            from .sim import ThermalSim

            sim = ThermalSim(
                cfg.sim, relays.state, (cfg.flow.cold, cfg.flow.hot), cfg.flow.specific_heat, mono, sim_seed
            )
            bus = FakeBus(sim, cfg.address_to_probe())
        storage = Storage(
            cfg.storage.db_path, cfg.storage.retention_hours, cfg.storage.archive_dir, cfg.storage.commit_s, mono
        )
        return ControlLoop(cfg, Sampler(cfg, bus, mono, wall), relays, storage, mono, wall, notify)
    except BaseException:
        relays.all_off()
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heatsteal-service", description="Run the heatsteal controller service")
    ap.add_argument("--config", default=os.environ.get("HEATSTEAL_CONFIG", str(config_mod.DEFAULT_PATH)))
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        log.critical("bad config: %s", e)
        return 2

    import uvicorn

    from .web import create_app

    loop = build(cfg)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):  # called by uvicorn's own signal capture
            loop.emergency_off()
            super().handle_exit(sig, frame)

    def on_signal(sig, frame):
        # Installed before uvicorn takes over and restored after it returns,
        # so a signal outside serve() still drops the relays and exits cleanly.
        loop.emergency_off()
        server.should_exit = True

    def on_crash():
        server.should_exit = True
        # If a graceful exit hangs, die anyway; systemd restarts us.
        threading.Timer(10.0, lambda: os._exit(1)).start()

    app = create_app(cfg, loop)
    # workers=1, deliberately: each worker is a separate process, and more
    # than one would mean several processes fighting over the GPIO pins.
    server = Server(uvicorn.Config(app, host=cfg.web.host, port=cfg.web.port, workers=1, log_level="warning"))
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    loop.on_crash = on_crash

    exit_code = 0
    try:
        loop.start()
        sd_notify("READY=1")
        log.info("heatsteal up on %s:%d (hardware=%s)", cfg.web.host, cfg.web.port, cfg.hardware)
        server.run()
    except BaseException:
        log.critical("service failed", exc_info=True)
        exit_code = 1
    finally:
        sd_notify("STOPPING=1")
        loop.shutdown()
    if loop.crashed:
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
