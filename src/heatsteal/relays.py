"""Relay drivers. SAFETY-CRITICAL.

The relay layer enforces its own invariant, independent of the state
machine (defence in depth):

    The Peltier may only be on while BOTH pumps are on.

Switching order: when turning things on, pumps first and the Peltier last;
when turning anything off, the Peltier first. A request that would break the
invariant forces the Peltier off and raises ``RelayInvariantError``.

The Waveshare board is active-low: driving a pin LOW energises the relay.
``GpioRelays`` expresses that with ``active_high=False`` so the rest of the
code only ever deals in "on"/"off". Only this module imports gpiozero, and
only when ``GpioRelays`` is constructed.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from .config import RelayPins
from .model import ALL_OFF, RelayOutputs

log = logging.getLogger(__name__)

CHANNELS = ("peltier", "cold_pump", "hot_pump")


class RelayInvariantError(RuntimeError):
    """Someone asked for the Peltier without both pumps."""


class Relays(Protocol):
    def apply(self, outputs: RelayOutputs) -> None: ...
    def all_off(self) -> None: ...
    def state(self) -> RelayOutputs: ...
    def close(self) -> None: ...


class _RelaysBase:
    """Ordering and invariant logic shared by the real and fake drivers.

    Subclasses implement ``_write(channel, on)`` and nothing else.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = {ch: False for ch in CHANNELS}

    def _write(self, channel: str, on: bool) -> None:
        raise NotImplementedError

    def _set(self, channel: str, on: bool) -> None:
        if self._state[channel] == on:
            return
        # Never let a pump drop while the Peltier is on, whoever asked.
        if not on and channel != "peltier" and self._state["peltier"]:
            self._set("peltier", False)
        if on and channel == "peltier" and not (self._state["cold_pump"] and self._state["hot_pump"]):
            raise RelayInvariantError("refusing to switch Peltier on without both pumps running")
        self._write(channel, on)
        self._state[channel] = on

    def apply(self, outputs: RelayOutputs) -> None:
        with self._lock:
            if outputs.peltier and not (outputs.cold_pump and outputs.hot_pump):
                self._set("peltier", False)
                raise RelayInvariantError(
                    f"invalid relay request {outputs.as_dict()}: Peltier needs both pumps; Peltier forced off"
                )
            try:
                # Off first, Peltier before pumps.
                if not outputs.peltier:
                    self._set("peltier", False)
                if not outputs.cold_pump:
                    self._set("cold_pump", False)
                if not outputs.hot_pump:
                    self._set("hot_pump", False)
                # Then on, pumps before Peltier.
                if outputs.cold_pump:
                    self._set("cold_pump", True)
                if outputs.hot_pump:
                    self._set("hot_pump", True)
                if outputs.peltier:
                    self._set("peltier", True)
            except BaseException:
                self._force_peltier_off()
                raise

    def _force_peltier_off(self) -> None:
        try:
            self._write("peltier", False)
        finally:
            self._state["peltier"] = False

    def all_off(self) -> None:
        """Peltier off, then both pumps off. Safe to call repeatedly, from any thread."""
        with self._lock:
            errors = []
            for ch in CHANNELS:  # peltier first
                try:
                    self._write(ch, False)
                    self._state[ch] = False
                except Exception as e:  # keep going: try to drop every channel
                    errors.append(e)
                    log.critical("failed to switch %s off: %s", ch, e)
            if errors:
                raise errors[0]

    def state(self) -> RelayOutputs:
        with self._lock:
            return RelayOutputs(**self._state)

    def close(self) -> None:
        self.all_off()


class FakeRelays(_RelaysBase):
    """In-memory relays. Records every write so tests can check switching order."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, bool]] = []

    def _write(self, channel: str, on: bool) -> None:
        self.writes.append((channel, on))


class GpioRelays(_RelaysBase):
    """Real relays on the Waveshare 3-channel board (active-low)."""

    def __init__(self, pins: RelayPins) -> None:
        super().__init__()
        from gpiozero import DigitalOutputDevice  # Pi only; imported lazily

        self._devices = {}
        try:
            for ch in CHANNELS:
                pin = getattr(pins, ch)
                # active_high=False: on() drives the pin LOW, which energises
                # the relay. initial_value=False: the pin comes up driven
                # HIGH, i.e. relay released.
                self._devices[ch] = DigitalOutputDevice(pin, active_high=False, initial_value=False)
                log.info("relay %s on BCM %d (active-low), initially off", ch, pin)
        except Exception:
            for dev in self._devices.values():
                dev.off()
            raise

    def _write(self, channel: str, on: bool) -> None:
        dev = self._devices[channel]
        if on:
            dev.on()
        else:
            dev.off()

    def close(self) -> None:
        # Leave the pins driven HIGH (relays off) rather than closing the
        # devices: closing releases the line, and what the pin does then is
        # exactly what acceptance test 3 (kill -9) checks.
        self.all_off()


def make_relays(hardware: str, pins: RelayPins) -> Relays:
    if hardware == "pi":
        return GpioRelays(pins)
    return FakeRelays()


__all__ = ["ALL_OFF", "FakeRelays", "GpioRelays", "RelayInvariantError", "Relays", "make_relays"]
