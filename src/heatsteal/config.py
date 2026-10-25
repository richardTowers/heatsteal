"""Config loading and validation.

The config is a TOML file (default ``/etc/heatsteal/config.toml``). Every
section maps onto a frozen dataclass. Unknown keys, wrong types and missing
sensor addresses are errors: a typo in a safety limit must stop the service
from starting, not be silently ignored.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

DEFAULT_PATH = Path("/etc/heatsteal/config.toml")

PROBES = ("cold_flow", "cold_return", "hot_flow", "hot_return")
LOOPS = ("cold", "hot")
ACTIONS = ("warn", "fault")

_ADDRESS_RE = re.compile(r"^28-[0-9a-f]{12}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RelayPins:
    """BCM pin numbers. The Waveshare board is active-low."""

    peltier: int = 21  # CH3
    cold_pump: int = 26  # CH1
    hot_pump: int = 20  # CH2


@dataclass(frozen=True)
class Flow:
    cold: float = 1.11  # g/s, measured by hand
    hot: float = 1.11  # g/s, measured by hand
    specific_heat: float = 4.18  # J/(g·K)


@dataclass(frozen=True)
class Timing:
    sample_s: float = 1.0
    sensor_stale_s: float = 5.0
    prime_s: float = 10.0
    cooldown_s: float = 60.0
    warmup_s: float = 120.0
    max_run_minutes: float = 120.0


@dataclass(frozen=True)
class Limits:
    hot_max_c: float = 41.0
    cold_min_c: float = 5.0
    hot_rise_max_k_per_min: float = 3.0
    cold_fall_max_k_per_min: float = 3.0
    loop_dt_max_k: float = 12.0
    # Peltier commanded on but a loop's ΔT stays below this for
    # min_loop_dt_hold_s: the 45 °C cutout may have tripped. Always a warning.
    min_loop_dt_k: float = 0.5
    min_loop_dt_hold_s: float = 30.0
    dead_pump_action: str = "warn"
    # Per-heuristic overrides. Empty means "use dead_pump_action".
    hot_rise_action: str = ""
    cold_fall_action: str = ""
    loop_dt_action: str = ""

    def action(self, heuristic: str) -> str:
        override = getattr(self, f"{heuristic}_action")
        return override or self.dead_pump_action


@dataclass(frozen=True)
class Storage:
    db_path: str = "/run/heatsteal/heatsteal.db"
    retention_hours: float = 24.0
    archive_dir: str = ""
    commit_s: float = 5.0


@dataclass(frozen=True)
class Web:
    host: str = "0.0.0.0"
    port: int = 8080
    control_token: str = ""
    grafana_url: str = "http://heatsteal.local:3000"


@dataclass(frozen=True)
class Sim:
    """Fake-hardware simulator settings. Ignored when hardware = "pi"."""

    ambient_c: float = 22.0
    # "none" | "stale_probe" | "dead_pump" | "runaway_hot"
    fault: str = "none"
    fault_probe: str = "hot_flow"  # for stale_probe
    fault_loop: str = "hot"  # for dead_pump
    fault_after_s: float = 0.0  # seconds after the Peltier first turns on
    noise: bool = True


@dataclass(frozen=True)
class Config:
    hardware: str = "fake"
    relays: RelayPins = field(default_factory=RelayPins)
    sensors: dict[str, str] = field(default_factory=dict)
    offsets: dict[str, float] = field(default_factory=dict)
    flow: Flow = field(default_factory=Flow)
    timing: Timing = field(default_factory=Timing)
    limits: Limits = field(default_factory=Limits)
    storage: Storage = field(default_factory=Storage)
    web: Web = field(default_factory=Web)
    sim: Sim = field(default_factory=Sim)

    def offset(self, probe: str) -> float:
        return self.offsets.get(probe, 0.0)

    def address_to_probe(self) -> dict[str, str]:
        return {addr: name for name, addr in self.sensors.items()}


def _coerce(section: str, key: str, value: Any, typ: Any) -> Any:
    where = f"[{section}] {key}" if section else key
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        return value
    if typ is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if typ is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
        return value
    raise AssertionError(f"unhandled type {typ}")


def _build_section(cls: type, section: str, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{section}] must be a table")
    hints = get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    kwargs = {k: _coerce(section, k, v, hints[k]) for k, v in raw.items()}
    return cls(**kwargs)


def from_dict(raw: dict[str, Any]) -> Config:
    raw = dict(raw)
    sections = {
        "relays": RelayPins,
        "flow": Flow,
        "timing": Timing,
        "limits": Limits,
        "storage": Storage,
        "web": Web,
        "sim": Sim,
    }
    known = set(sections) | {"hardware", "sensors", "offsets"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    kwargs: dict[str, Any] = {}
    if "hardware" in raw:
        kwargs["hardware"] = _coerce("", "hardware", raw["hardware"], str)
    for name, cls in sections.items():
        if name in raw:
            kwargs[name] = _build_section(cls, name, raw[name])

    sensors = raw.get("sensors")
    if not isinstance(sensors, dict):
        raise ConfigError("[sensors] is required and must map each probe to a 1-Wire address")
    kwargs["sensors"] = {k: _coerce("sensors", k, v, str) for k, v in sensors.items()}

    offsets = raw.get("offsets", {})
    if not isinstance(offsets, dict):
        raise ConfigError("[offsets] must be a table")
    kwargs["offsets"] = {k: _coerce("offsets", k, v, float) for k, v in offsets.items()}

    cfg = Config(**kwargs)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.hardware not in ("fake", "pi"):
        raise ConfigError(f'hardware must be "fake" or "pi", got {cfg.hardware!r}')

    pins = [cfg.relays.peltier, cfg.relays.cold_pump, cfg.relays.hot_pump]
    for pin in pins:
        if not 0 <= pin <= 27:
            raise ConfigError(f"[relays] BCM pin {pin} out of range 0-27")
    if len(set(pins)) != 3:
        raise ConfigError("[relays] each load needs its own pin")
    if 4 in pins:
        raise ConfigError("[relays] BCM 4 is the 1-Wire bus")

    unknown = set(cfg.sensors) - set(PROBES)
    if unknown:
        raise ConfigError(f"[sensors] unknown probe(s): {', '.join(sorted(unknown))}")
    missing = [p for p in PROBES if p not in cfg.sensors]
    if missing:
        raise ConfigError(f"[sensors] missing address for: {', '.join(missing)}")
    for name, addr in cfg.sensors.items():
        if not _ADDRESS_RE.match(addr):
            raise ConfigError(f"[sensors] {name}: {addr!r} is not a DS18B20 address (28-xxxxxxxxxxxx)")
    if len(set(cfg.sensors.values())) != len(PROBES):
        raise ConfigError("[sensors] two probes share an address")

    unknown = set(cfg.offsets) - set(PROBES)
    if unknown:
        raise ConfigError(f"[offsets] unknown probe(s): {', '.join(sorted(unknown))}")
    for name, off in cfg.offsets.items():
        if abs(off) > 5:
            raise ConfigError(f"[offsets] {name}: {off} °C is implausibly large; recalibrate")

    if cfg.flow.cold <= 0 or cfg.flow.hot <= 0:
        raise ConfigError("[flow] flow rates must be positive")
    if cfg.flow.specific_heat <= 0:
        raise ConfigError("[flow] specific_heat must be positive")

    t = cfg.timing
    for name in ("sample_s", "sensor_stale_s", "prime_s", "cooldown_s", "warmup_s", "max_run_minutes"):
        if getattr(t, name) <= 0:
            raise ConfigError(f"[timing] {name} must be positive")
    if t.sensor_stale_s < 2 * t.sample_s:
        raise ConfigError("[timing] sensor_stale_s must be at least twice sample_s")

    lim = cfg.limits
    if lim.cold_min_c >= lim.hot_max_c:
        raise ConfigError("[limits] cold_min_c must be below hot_max_c")
    if lim.hot_max_c > 44.0:
        raise ConfigError("[limits] hot_max_c above 44 °C risks skin injury")
    for name in (
        "hot_rise_max_k_per_min",
        "cold_fall_max_k_per_min",
        "loop_dt_max_k",
        "min_loop_dt_k",
        "min_loop_dt_hold_s",
    ):
        if getattr(lim, name) <= 0:
            raise ConfigError(f"[limits] {name} must be positive")
    if lim.dead_pump_action not in ACTIONS:
        raise ConfigError('[limits] dead_pump_action must be "warn" or "fault"')
    for h in ("hot_rise", "cold_fall", "loop_dt"):
        v = getattr(lim, f"{h}_action")
        if v and v not in ACTIONS:
            raise ConfigError(f'[limits] {h}_action must be "", "warn" or "fault"')

    if cfg.storage.retention_hours <= 0:
        raise ConfigError("[storage] retention_hours must be positive")
    if cfg.storage.commit_s <= 0:
        raise ConfigError("[storage] commit_s must be positive")

    if not 1 <= cfg.web.port <= 65535:
        raise ConfigError("[web] port out of range")

    if cfg.sim.fault not in ("none", "stale_probe", "dead_pump", "runaway_hot"):
        raise ConfigError(f"[sim] unknown fault {cfg.sim.fault!r}")
    if cfg.sim.fault_probe not in PROBES:
        raise ConfigError(f"[sim] fault_probe must be one of {', '.join(PROBES)}")
    if cfg.sim.fault_loop not in LOOPS:
        raise ConfigError('[sim] fault_loop must be "cold" or "hot"')


def load(path: str | Path = DEFAULT_PATH) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    try:
        return from_dict(raw)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None
