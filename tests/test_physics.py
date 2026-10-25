import pytest

from heatsteal.physics import EnergyIntegrator, power_cold, power_hot


def test_power_cold_positive_when_return_warmer():
    # 1.1 g/s * 4.18 J/(g·K) * 4.5 K
    assert power_cold(1.1, 4.18, cold_flow_c=20.0, cold_return_c=24.5) == pytest.approx(20.691)


def test_power_hot_positive_when_flow_warmer():
    assert power_hot(1.1, 4.18, hot_flow_c=36.0, hot_return_c=31.5) == pytest.approx(20.691)


def test_power_signs_reverse():
    assert power_cold(1.0, 4.18, 25.0, 20.0) < 0
    assert power_hot(1.0, 4.18, 30.0, 35.0) < 0


def test_trapezoid_constant():
    e = EnergyIntegrator()
    for t in range(11):
        e.add(float(t), 20.0)
    assert e.joules == pytest.approx(200.0)
    assert e.kilojoules == pytest.approx(0.2)


def test_trapezoid_ramp():
    e = EnergyIntegrator()
    e.add(0.0, 0.0)
    e.add(10.0, 10.0)  # triangle: 0.5 * 10 * 10
    assert e.joules == pytest.approx(50.0)


def test_gap_is_not_integrated():
    e = EnergyIntegrator()
    e.add(0.0, 10.0)
    e.add(1.0, 10.0)
    e.add(2.0, None)
    e.add(5.0, 10.0)  # restart: no area across the gap
    e.add(6.0, 10.0)
    assert e.joules == pytest.approx(20.0)


def test_reset():
    e = EnergyIntegrator()
    e.add(0.0, 10.0)
    e.add(1.0, 10.0)
    e.reset()
    assert e.joules == 0
    e.add(2.0, 10.0)
    assert e.joules == 0
