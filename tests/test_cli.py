import subprocess
import sys

import pytest

from heatsteal import cli


def test_cli_does_not_import_gpio_code():
    code = "import sys, heatsteal.cli; print('heatsteal.relays' in sys.modules, 'gpiozero' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False False"


def test_compute_offsets():
    samples = [
        {"cold_flow": 20.0, "cold_return": 20.5, "hot_flow": 19.5, "hot_return": 20.0},
        {"cold_flow": 21.0, "cold_return": 21.5, "hot_flow": 20.5, "hot_return": 21.0},
        {"cold_flow": None, "cold_return": 99.0, "hot_flow": 99.0, "hot_return": 99.0},  # ignored
    ]
    off = cli.compute_offsets(samples)
    assert off == pytest.approx({"cold_flow": 0.0, "cold_return": -0.5, "hot_flow": 0.5, "hot_return": 0.0})
    assert sum(off.values()) == pytest.approx(0.0)


def test_format_offsets_is_valid_toml():
    import tomllib

    text = cli.format_offsets({"cold_flow": 0.1, "cold_return": -0.2, "hot_flow": 0.05, "hot_return": 0.05})
    assert tomllib.loads(text)["offsets"]["cold_return"] == pytest.approx(-0.2)


def test_format_status():
    s = {
        "state": "FAULT", "time_in_state_s": 3.0, "p_cold_w": 20.0, "p_hot_w": None, "kj_cold": 1.5,
        "kj_hot": 0.0, "last_fault": "hot_flow too hot", "warnings": ["w1"], "pump_job": None,
        "probes": {"hot_flow": {"celsius": None, "stale": True, "error": "probe missing"}},
        "relays": {"peltier": False, "cold_pump": True, "hot_pump": True},
    }
    text = cli.format_status(s)
    assert "FAULT" in text and "hot_flow too hot" in text and "STALE" in text and "WARNING" in text


def test_api_unreachable():
    with pytest.raises(cli.ApiError, match="cannot reach"):
        cli.api("http://127.0.0.1:9", "/api/state", timeout=1)


def test_sensors_refuses_when_service_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "service_running", lambda url: "the API answers")
    assert cli.main(["--url", "http://x", "sensors"]) == 1
    assert "refusing" in capsys.readouterr().err
    assert cli.main(["--url", "http://x", "calibrate", "--minutes", "0.1"]) == 1


def test_sensors_lists_fake_bus(monkeypatch, capsys, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(cli, "service_running", lambda url: None)
    example = Path(__file__).parent.parent / "config.example.toml"
    assert cli.main(["--config", str(example), "--url", "http://x", "sensors"]) == 0
    out = capsys.readouterr().out
    assert "28-000000000003" in out and "hot_flow" in out
