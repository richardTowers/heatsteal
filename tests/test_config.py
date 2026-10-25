import pytest

from heatsteal import config as config_mod
from heatsteal.config import ConfigError

from .conftest import ADDRESSES, make_config


def test_example_config_loads():
    from pathlib import Path

    cfg = config_mod.load(Path(__file__).parent.parent / "config.example.toml")
    assert cfg.hardware == "fake"
    assert cfg.relays.peltier == 26 and cfg.relays.cold_pump == 20 and cfg.relays.hot_pump == 21
    assert cfg.limits.hot_max_c == 41.0
    assert cfg.timing.sensor_stale_s == 5.0
    assert cfg.flow.specific_heat == 4.18


def test_defaults(cfg):
    assert cfg.timing.prime_s == 10
    assert cfg.limits.dead_pump_action == "warn"
    assert cfg.limits.action("hot_rise") == "warn"
    assert cfg.offset("cold_flow") == 0.0


def test_per_heuristic_override():
    cfg = make_config(limits={"loop_dt_action": "fault"})
    assert cfg.limits.action("loop_dt") == "fault"
    assert cfg.limits.action("hot_rise") == "warn"


@pytest.mark.parametrize(
    "raw, msg",
    [
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "bogus": 1}, "unknown top-level"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "limits": {"hot_max": 40}}, "unknown key"),
        ({"hardware": "fake"}, "[sensors] is required"),
        ({"hardware": "fake", "sensors": {k: v for k, v in ADDRESSES.items() if k != "hot_flow"}}, "missing address for: hot_flow"),
        ({"hardware": "fake", "sensors": {**ADDRESSES, "hot_flow": "28-000000000001"}}, "share an address"),
        ({"hardware": "fake", "sensors": {**ADDRESSES, "hot_flow": "nonsense"}}, "not a DS18B20"),
        ({"hardware": "gpio", "sensors": dict(ADDRESSES)}, "hardware must be"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "relays": {"peltier": 20}}, "own pin"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "limits": {"hot_max_c": "41"}}, "expected a number"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "limits": {"hot_max_c": 46.0}}, "44"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "limits": {"dead_pump_action": "ignore"}}, "dead_pump_action"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "offsets": {"hot_flw": 0.1}}, "unknown probe"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "flow": {"cold": 0}}, "positive"),
        ({"hardware": "fake", "sensors": dict(ADDRESSES), "web": {"port": True}}, "integer"),
    ],
)
def test_rejects(raw, msg):
    with pytest.raises(ConfigError, match=msg.replace("[", r"\[")):
        config_mod.from_dict(raw)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        config_mod.load(tmp_path / "nope.toml")


def test_int_accepted_for_float():
    cfg = make_config(timing={"cooldown_s": 30})
    assert cfg.timing.cooldown_s == 30.0 and isinstance(cfg.timing.cooldown_s, float)
