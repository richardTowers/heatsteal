"""Heat-flow arithmetic. Pure functions, no I/O.

Sign conventions: both powers are positive when the rig is doing its job.
P_cold is heat taken *from* the headband wearer (the return is warmer than
the flow); P_hot is heat given *to* the vest wearer (the flow is warmer than
the return).
"""

from __future__ import annotations

from dataclasses import dataclass


def power_cold(m_dot_g_s: float, c_j_gk: float, cold_flow_c: float, cold_return_c: float) -> float:
    """Watts stolen from the headband wearer."""
    return m_dot_g_s * c_j_gk * (cold_return_c - cold_flow_c)


def power_hot(m_dot_g_s: float, c_j_gk: float, hot_flow_c: float, hot_return_c: float) -> float:
    """Watts delivered to the vest wearer."""
    return m_dot_g_s * c_j_gk * (hot_flow_c - hot_return_c)


@dataclass
class EnergyIntegrator:
    """Trapezoidal integral of power over time.

    A missing power sample (a probe read failed) breaks the trapezoid: the
    gap is not integrated, rather than guessing a value across it.
    """

    joules: float = 0.0
    _last_t: float | None = None
    _last_p: float | None = None

    def reset(self) -> None:
        self.joules = 0.0
        self._last_t = None
        self._last_p = None

    def add(self, t: float, power_w: float | None) -> float:
        if power_w is None:
            self._last_t = None
            self._last_p = None
            return self.joules
        if self._last_t is not None and self._last_p is not None and t > self._last_t:
            self.joules += 0.5 * (self._last_p + power_w) * (t - self._last_t)
        self._last_t = t
        self._last_p = power_w
        return self.joules

    @property
    def kilojoules(self) -> float:
        return self.joules / 1000.0
