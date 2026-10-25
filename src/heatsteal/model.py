"""Plain data types shared across the service. No I/O here."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class State(enum.StrEnum):
    IDLE = "IDLE"
    PRIMING = "PRIMING"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    FAULT = "FAULT"


@dataclass(frozen=True)
class RelayOutputs:
    peltier: bool = False
    cold_pump: bool = False
    hot_pump: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {"peltier": self.peltier, "cold_pump": self.cold_pump, "hot_pump": self.hot_pump}


ALL_OFF = RelayOutputs()


@dataclass(frozen=True)
class Command:
    action: str  # "start" | "stop" | "reset" | "pump"
    loop: str | None = None  # for "pump"
    seconds: float | None = None  # for "pump"

    def describe(self) -> str:
        if self.action == "pump":
            secs = "?" if self.seconds is None else f"{self.seconds:g}"
            return f"pump {self.loop} {secs}s"
        return self.action


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    state: State
    reason: str = ""


EVENT_KINDS = ("state", "command", "fault", "warn", "info")


@dataclass(frozen=True)
class Event:
    kind: str  # one of EVENT_KINDS
    detail: str
    ts: float | None = None  # wall clock; filled in by the service if None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"bad event kind {self.kind!r}")


@dataclass(frozen=True)
class ProbeReading:
    name: str
    address: str
    celsius: float | None  # this sample, offset applied; None if the read failed
    last_good_c: float | None  # most recent good value, offset applied
    last_good_mono: float | None  # monotonic time of last good value
    stale: bool
    error: str = ""  # why this sample was rejected, if it was

    @property
    def usable(self) -> float | None:
        """Most recent good value, or None if the probe is stale."""
        return None if self.stale else self.last_good_c


@dataclass(frozen=True)
class Snapshot:
    """One sample of the whole rig, published by the sampler. Immutable."""

    ts: float  # wall clock, Unix epoch seconds (UTC), for storage/display
    mono: float  # monotonic seconds, for all control timing
    probes: dict[str, ProbeReading]
    p_cold: float | None  # W stolen from the headband wearer
    p_hot: float | None  # W delivered to the vest wearer

    def all_fresh(self) -> bool:
        return all(p.last_good_c is not None and not p.stale for p in self.probes.values())

    def stale_probes(self) -> list[str]:
        return [name for name, p in self.probes.items() if p.stale]

    def value(self, name: str) -> float | None:
        p = self.probes.get(name)
        return None if p is None else p.usable
