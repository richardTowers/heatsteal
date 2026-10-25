"""DS18B20 temperature probes on the 1-Wire bus.

``SysfsBus`` talks to the kernel's w1_therm driver. Per the kernel docs
(Documentation/w1/slaves/w1_therm.rst):

* Writing ``trigger`` to ``w1_bus_master1/therm_bulk_read`` starts a
  conversion on every probe at once.
* Reading ``therm_bulk_read`` returns ``-1`` while at least one probe is
  still converting, ``1`` once conversion is complete but some values are
  unread, and ``0`` when no bulk conversion is pending.
* Each device's ``temperature`` file then returns the converted value in
  integer millidegrees, and "may return empty if conversion is still in
  progress".
* The older ``w1_slave`` file triggers its own conversion on every read and
  gives two lines: the scratchpad bytes with ``crc=xx YES|NO``, then the
  same bytes with ``t=<millidegrees>``.

This module never imports anything GPIO-related, so the CLI can use it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)

W1_DEVICES = Path("/sys/bus/w1/devices")

POWER_ON_RESET_C = 85.0
DISCONNECTED_C = -127.0
MIN_PLAUSIBLE_C = -20.0
MAX_PLAUSIBLE_C = 90.0


class SensorError(ValueError):
    """A reading that must be treated as missing."""


@dataclass(frozen=True)
class RawReading:
    celsius: float | None  # raw, no offset; None if rejected
    error: str = ""


class SensorBus(Protocol):
    def addresses(self) -> list[str]: ...
    def read(self, addresses: list[str]) -> dict[str, RawReading]: ...


def validate_celsius(c: float) -> float:
    if c == POWER_ON_RESET_C:
        raise SensorError("85.000 °C power-on reset value")
    if c == DISCONNECTED_C:
        raise SensorError("-127 °C (probe disconnected)")
    if c < MIN_PLAUSIBLE_C or c > MAX_PLAUSIBLE_C:
        raise SensorError(f"{c:.3f} °C out of plausible range")
    return c


def parse_temperature_file(text: str) -> float:
    """Parse a w1_therm ``temperature`` file (integer millidegrees)."""
    text = text.strip()
    if not text:
        raise SensorError("empty temperature file (conversion not finished?)")
    try:
        milli = int(text)
    except ValueError:
        raise SensorError(f"unparseable temperature {text!r}") from None
    return validate_celsius(milli / 1000.0)


def parse_w1_slave(text: str) -> float:
    """Parse a w1_therm ``w1_slave`` file, checking the CRC line."""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        raise SensorError("truncated w1_slave output")
    if not lines[0].strip().endswith("YES"):
        raise SensorError("CRC check failed")
    _, sep, value = lines[1].rpartition("t=")
    if not sep:
        raise SensorError("no t= in w1_slave output")
    try:
        milli = int(value.strip())
    except ValueError:
        raise SensorError(f"unparseable t={value!r}") from None
    return validate_celsius(milli / 1000.0)


class SysfsBus:
    def __init__(
        self,
        root: Path = W1_DEVICES,
        timeout_s: float = 1.0,
        poll_s: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(root)
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self._bulk_ok: bool | None = None  # None = not yet tried

    @property
    def _bulk_file(self) -> Path:
        return self.root / "w1_bus_master1" / "therm_bulk_read"

    def addresses(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("28-*"))

    def _bulk_status(self) -> str:
        return self._bulk_file.read_text().strip()

    def _bulk_convert(self) -> bool:
        """Trigger a simultaneous conversion. False if bulk read is unavailable."""
        if self._bulk_ok is False:
            return False
        try:
            self._bulk_file.write_text("trigger\n")
        except (FileNotFoundError, PermissionError) as e:
            # Logged once; from now on every sample uses w1_slave. Note that
            # w1_slave converts each probe in turn (~750 ms each), so the
            # probes are no longer sampled at the same instant.
            self._bulk_ok = False
            log.warning("therm_bulk_read unavailable (%s); falling back to per-probe w1_slave reads", e)
            return False
        except OSError as e:
            log.warning("bulk trigger failed (%s); using w1_slave for this sample", e)
            return False
        self._bulk_ok = True
        deadline = self._clock() + self.timeout_s
        while True:
            try:
                status = self._bulk_status()
            except OSError as e:
                log.warning("reading therm_bulk_read failed: %s", e)
                return True  # still try the temperature files; they validate themselves
            if status != "-1":
                return True
            if self._clock() >= deadline:
                log.warning("bulk conversion did not finish within %.1fs", self.timeout_s)
                return True
            self._sleep(self.poll_s)

    def _read_one(self, address: str, bulk: bool) -> RawReading:
        dev = self.root / address
        try:
            if bulk:
                return RawReading(parse_temperature_file((dev / "temperature").read_text()))
            return RawReading(parse_w1_slave((dev / "w1_slave").read_text()))
        except FileNotFoundError:
            return RawReading(None, "probe missing from bus")
        except OSError as e:
            # w1_therm reports CRC failures on the temperature file as an I/O error.
            return RawReading(None, f"read error: {e.strerror or e}")
        except SensorError as e:
            return RawReading(None, str(e))

    def read(self, addresses: list[str]) -> dict[str, RawReading]:
        bulk = self._bulk_convert()
        return {addr: self._read_one(addr, bulk) for addr in addresses}


class FakeBus:
    """Probe readings from the simulator (``sim.py``)."""

    def __init__(self, sim, address_to_probe: dict[str, str]) -> None:
        self.sim = sim
        self._addr_to_probe = dict(address_to_probe)

    def addresses(self) -> list[str]:
        return sorted(self._addr_to_probe)

    def read(self, addresses: list[str]) -> dict[str, RawReading]:
        self.sim.advance()
        values = self.sim.read_probes()
        out = {}
        for addr in addresses:
            probe = self._addr_to_probe.get(addr)
            v = values.get(probe) if probe else None
            if v is None:
                out[addr] = RawReading(None, "probe missing from bus")
                continue
            try:
                out[addr] = RawReading(validate_celsius(v))
            except SensorError as e:
                out[addr] = RawReading(None, str(e))
        return out
