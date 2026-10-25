"""A crude lumped thermal model of the rig, for developing without hardware.

Each loop is a lump of water exchanging heat with a wearer, with the room,
and (while its pump runs) with one face of the Peltier. Each Peltier face is
a small block: with flow, it sits at the loop's exit temperature; without
flow, it heats or cools on its own. The probes are first-order lags towards
the water passing them, or towards the room when the water is still.

The 45 °C bimetallic cutout on the hot block is modelled too.

Faults (config ``[sim]``), all starting ``fault_after_s`` after the Peltier
first switches on:

* ``stale_probe``: ``fault_probe`` stops answering.
* ``dead_pump``: ``fault_loop``'s pump silently stops; its relay stays on.
* ``runaway_hot``: the hot side gains heat much faster than the vest sheds it.
"""

from __future__ import annotations

import random
import time
from typing import Callable

from .config import Sim as SimConfig
from .model import RelayOutputs

# Rough figures; the point is plausible behaviour, not accuracy.
Q_COLD_W = 20.0  # heat pumped out of the cold loop with the Peltier on
Q_HOT_W = 24.0  # heat dumped into the hot loop with the Peltier on
LOOP_MASS_G = 300.0
SKIN_C = 33.0
G_HEADBAND_W_K = 6.0
G_VEST_W_K = 10.0
G_LOOP_AMBIENT_W_K = 1.0
BLOCK_HEAT_CAP_J_K = 150.0
G_BLOCK_AMBIENT_W_K = 0.5
PROBE_TAU_FLOWING_S = 4.0
PROBE_TAU_STILL_S = 30.0
CUTOUT_OPEN_C = 45.0
CUTOUT_CLOSE_C = 35.0
RUNAWAY_FACTOR = 3.0
DS18B20_STEP_C = 0.0625
MAX_STEP_S = 0.25


class ThermalSim:
    def __init__(
        self,
        cfg: SimConfig,
        relay_state: Callable[[], RelayOutputs],
        flow_g_s: tuple[float, float] = (1.11, 1.11),
        specific_heat: float = 4.18,
        clock: Callable[[], float] = time.monotonic,
        seed: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.relay_state = relay_state
        self.m_cold, self.m_hot = flow_g_s
        self.c = specific_heat
        self.clock = clock
        self.rng = random.Random(seed)
        amb = cfg.ambient_c
        self.loop_cold = amb
        self.loop_hot = amb
        self.block_cold = amb
        self.block_hot = amb
        self.probes = {"cold_flow": amb, "cold_return": amb, "hot_flow": amb, "hot_return": amb}
        self.cutout_open = False
        self.peltier_on_since: float | None = None  # sim time of first Peltier-on
        self.t = 0.0
        self._last_clock: float | None = None

    # -- faults -----------------------------------------------------------

    def _fault_active(self, kind: str) -> bool:
        if self.cfg.fault != kind or self.peltier_on_since is None:
            return False
        return self.t - self.peltier_on_since >= self.cfg.fault_after_s

    # -- stepping ---------------------------------------------------------

    def advance(self) -> None:
        now = self.clock()
        if self._last_clock is None:
            self._last_clock = now
            return
        dt = now - self._last_clock
        self._last_clock = now
        while dt > 0:
            step = min(dt, MAX_STEP_S)
            self.step(step)
            dt -= step

    def step(self, dt: float) -> None:
        relays = self.relay_state()
        self.t += dt
        if relays.peltier and self.peltier_on_since is None:
            self.peltier_on_since = self.t

        amb = self.cfg.ambient_c
        cold_flowing = relays.cold_pump and not (self._fault_active("dead_pump") and self.cfg.fault_loop == "cold")
        hot_flowing = relays.hot_pump and not (self._fault_active("dead_pump") and self.cfg.fault_loop == "hot")

        peltier = relays.peltier and not self.cutout_open
        q_c = Q_COLD_W if peltier else 0.0
        q_h = Q_HOT_W if peltier else 0.0
        g_vest = G_VEST_W_K
        if self._fault_active("runaway_hot"):
            q_h *= RUNAWAY_FACTOR
            g_vest *= 0.3
        heat_cap = LOOP_MASS_G * self.c

        # Cold side
        if cold_flowing:
            exch = q_c
            self.block_cold = self.loop_cold - q_c / (self.m_cold * self.c)
        else:
            exch = 0.0
            self.block_cold += dt * (-q_c + G_BLOCK_AMBIENT_W_K * (amb - self.block_cold)) / BLOCK_HEAT_CAP_J_K
        self.loop_cold += dt * (
            G_HEADBAND_W_K * (SKIN_C - self.loop_cold) + G_LOOP_AMBIENT_W_K * (amb - self.loop_cold) - exch
        ) / heat_cap

        # Hot side
        if hot_flowing:
            exch = q_h
            self.block_hot = self.loop_hot + q_h / (self.m_hot * self.c)
        else:
            exch = 0.0
            self.block_hot += dt * (q_h + G_BLOCK_AMBIENT_W_K * (amb - self.block_hot)) / BLOCK_HEAT_CAP_J_K
        self.loop_hot += dt * (
            g_vest * (SKIN_C - self.loop_hot) + G_LOOP_AMBIENT_W_K * (amb - self.loop_hot) + exch
        ) / heat_cap

        # Bimetallic cutout in series with the Peltier
        if self.block_hot >= CUTOUT_OPEN_C:
            self.cutout_open = True
        elif self.block_hot <= CUTOUT_CLOSE_C:
            self.cutout_open = False

        # Probes, just outside the box
        targets = {
            "cold_flow": self.block_cold if cold_flowing else 0.5 * (self.block_cold + amb),
            "cold_return": self.loop_cold if cold_flowing else amb,
            "hot_flow": self.block_hot if hot_flowing else 0.5 * (self.block_hot + amb),
            "hot_return": self.loop_hot if hot_flowing else amb,
        }
        for name, target in targets.items():
            flowing = cold_flowing if name.startswith("cold") else hot_flowing
            tau = PROBE_TAU_FLOWING_S if flowing else PROBE_TAU_STILL_S
            self.probes[name] += (target - self.probes[name]) * min(1.0, dt / tau)

    def read_probes(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for name, v in self.probes.items():
            if self._fault_active("stale_probe") and name == self.cfg.fault_probe:
                out[name] = None
                continue
            if self.cfg.noise:
                v += self.rng.gauss(0.0, 0.03)
            out[name] = round(v / DS18B20_STEP_C) * DS18B20_STEP_C
        return out
