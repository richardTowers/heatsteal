import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from heatsteal import service
from heatsteal.sensors import RawReading

from .conftest import ADDRESSES, make_config


def test_sd_notify(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert service.sd_notify("WATCHDOG=1")
        assert s.recv(100) == b"WATCHDOG=1"


def test_sd_notify_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert service.sd_notify("WATCHDOG=1") is False


class ScriptBus:
    def __init__(self):
        self.values = {}

    def addresses(self):
        return list(ADDRESSES.values())

    def read(self, addresses):
        return {
            a: RawReading(self.values.get(a)) if self.values.get(a) is not None else RawReading(None, "gone")
            for a in addresses
        }


def test_sampler_offsets_power_and_staleness():
    cfg = make_config(offsets={"cold_return": 0.5}, flow={"cold": 1.0, "hot": 2.0, "specific_heat": 4.0})
    bus = ScriptBus()
    t = [0.0]
    s = service.Sampler(cfg, bus, mono=lambda: t[0], wall=lambda: 1e9 + t[0])
    bus.values = {
        ADDRESSES["cold_flow"]: 20.0,
        ADDRESSES["cold_return"]: 24.0,
        ADDRESSES["hot_flow"]: 35.0,
        ADDRESSES["hot_return"]: 31.0,
    }
    snap = s.sample()
    assert snap.probes["cold_return"].celsius == pytest.approx(24.5)  # offset applied
    assert snap.p_cold == pytest.approx(1.0 * 4.0 * 4.5)
    assert snap.p_hot == pytest.approx(2.0 * 4.0 * 4.0)
    assert snap.all_fresh()

    del bus.values[ADDRESSES["hot_flow"]]
    t[0] = 3.0
    snap = s.sample()
    p = snap.probes["hot_flow"]
    assert p.celsius is None and p.last_good_c == 35.0 and not p.stale
    assert snap.p_hot is None  # no power from a missing reading
    t[0] = 5.5
    snap = s.sample()
    assert snap.probes["hot_flow"].stale
    assert snap.value("hot_flow") is None


def test_never_read_probe_goes_stale_after_grace():
    cfg = make_config()
    bus = ScriptBus()
    t = [0.0]
    s = service.Sampler(cfg, bus, mono=lambda: t[0])
    snap = s.sample()
    assert not snap.probes["hot_flow"].stale and not snap.all_fresh()
    t[0] = 6.0
    assert s.sample().probes["hot_flow"].stale


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_process_exits_cleanly_on_signal(tmp_path, sig):
    port = _free_port()
    cfgfile = tmp_path / "config.toml"
    sensors = "\n".join(f'{k} = "{v}"' for k, v in ADDRESSES.items())
    cfgfile.write_text(
        f'hardware = "fake"\n[sensors]\n{sensors}\n[storage]\ndb_path = "{tmp_path / "h.db"}"\n'
        f'[web]\nhost = "127.0.0.1"\nport = {port}\n'
    )
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    p = subprocess.Popen(
        [sys.executable, "-m", "heatsteal.service", "--config", str(cfgfile)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1).read()
                break
            except OSError:
                if time.monotonic() > deadline or p.poll() is not None:
                    raise AssertionError(p.communicate(timeout=5)[0]) from None
                time.sleep(0.1)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/command",
            data=b'{"action":"start"}',
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
        p.send_signal(sig)
        out, _ = p.communicate(timeout=15)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, out
    assert "service stopped; relays off" in out
