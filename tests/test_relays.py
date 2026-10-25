import sys

import pytest

from heatsteal.model import RelayOutputs
from heatsteal.relays import FakeRelays, RelayInvariantError

ALL_ON = RelayOutputs(True, True, True)
PUMPS = RelayOutputs(False, True, True)


@pytest.mark.parametrize("bad", [
    RelayOutputs(True, False, False),
    RelayOutputs(True, True, False),
    RelayOutputs(True, False, True),
])
def test_peltier_requires_both_pumps(bad):
    r = FakeRelays()
    with pytest.raises(RelayInvariantError):
        r.apply(bad)
    assert r.state().peltier is False
    assert ("peltier", True) not in r.writes


def test_bad_request_forces_running_peltier_off():
    r = FakeRelays()
    r.apply(ALL_ON)
    with pytest.raises(RelayInvariantError):
        r.apply(RelayOutputs(True, True, False))
    assert r.state().peltier is False
    assert r.writes[-1] == ("peltier", False)


def test_on_order_pumps_then_peltier():
    r = FakeRelays()
    r.apply(ALL_ON)
    assert r.writes == [("cold_pump", True), ("hot_pump", True), ("peltier", True)]


def test_off_order_peltier_first():
    r = FakeRelays()
    r.apply(ALL_ON)
    r.writes.clear()
    r.apply(RelayOutputs())
    assert r.writes[0] == ("peltier", False)
    assert set(r.writes[1:]) == {("cold_pump", False), ("hot_pump", False)}


def test_dropping_one_pump_drops_peltier_first():
    r = FakeRelays()
    r.apply(ALL_ON)
    r.writes.clear()
    with pytest.raises(RelayInvariantError):
        r.apply(RelayOutputs(True, False, True))
    assert r.writes == [("peltier", False)]


def test_running_to_cooldown_only_switches_peltier():
    r = FakeRelays()
    r.apply(ALL_ON)
    r.writes.clear()
    r.apply(PUMPS)
    assert r.writes == [("peltier", False)]
    assert r.state() == PUMPS


def test_all_off():
    r = FakeRelays()
    r.apply(ALL_ON)
    r.writes.clear()
    r.all_off()
    assert r.writes == [("peltier", False), ("cold_pump", False), ("hot_pump", False)]
    assert r.state() == RelayOutputs()
    r.all_off()  # idempotent


def test_write_failure_forces_peltier_off():
    class Flaky(FakeRelays):
        def _write(self, channel, on):
            if channel == "hot_pump" and on:
                raise OSError("gpio broke")
            super()._write(channel, on)

    r = Flaky()
    with pytest.raises(OSError):
        r.apply(ALL_ON)
    assert r.state().peltier is False
    assert ("peltier", True) not in r.writes


def test_all_off_keeps_going_after_a_failure():
    class Flaky(FakeRelays):
        def _write(self, channel, on):
            if channel == "peltier":
                raise OSError("gpio broke")
            super()._write(channel, on)

    r = Flaky()
    r.apply(PUMPS)
    with pytest.raises(OSError):
        r.all_off()
    assert ("cold_pump", False) in r.writes and ("hot_pump", False) in r.writes


def test_gpio_relays_active_low(monkeypatch):
    """GpioRelays must build each device as active-low and initially off."""
    import types

    from heatsteal.config import RelayPins
    from heatsteal.relays import GpioRelays

    made = []

    class Dev:
        def __init__(self, pin, active_high, initial_value):
            made.append((pin, active_high, initial_value))
            self.value = 0

        def on(self):
            self.value = 1

        def off(self):
            self.value = 0

    monkeypatch.setitem(sys.modules, "gpiozero", types.SimpleNamespace(DigitalOutputDevice=Dev))
    r = GpioRelays(RelayPins())
    assert sorted(made) == [(20, False, False), (21, False, False), (26, False, False)]
    r.apply(ALL_ON)
    assert all(d.value == 1 for d in r._devices.values())
    r.close()
    assert all(d.value == 0 for d in r._devices.values())
