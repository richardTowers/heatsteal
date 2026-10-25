import pytest

from heatsteal.sensors import SensorError, SysfsBus, parse_temperature_file, parse_w1_slave

from .conftest import FIXTURES


def fx(name):
    return (FIXTURES / name).read_text()


def test_w1_slave_ok():
    assert parse_w1_slave(fx("w1_slave_ok")) == pytest.approx(23.125)


def test_w1_slave_negative():
    assert parse_w1_slave(fx("w1_slave_negative")) == pytest.approx(-0.125)


@pytest.mark.parametrize(
    "name, msg",
    [
        ("w1_slave_crc_fail", "CRC"),
        ("w1_slave_disconnected", "CRC"),
        ("w1_slave_85", "power-on reset"),
        ("w1_slave_minus127", "-127"),
    ],
)
def test_w1_slave_rejects(name, msg):
    with pytest.raises(SensorError, match=msg):
        parse_w1_slave(fx(name))


def test_w1_slave_truncated():
    with pytest.raises(SensorError):
        parse_w1_slave("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n")


def test_temperature_ok():
    assert parse_temperature_file(fx("temperature_ok")) == pytest.approx(23.125)


@pytest.mark.parametrize(
    "name, msg",
    [
        ("temperature_85", "power-on reset"),
        ("temperature_minus127", "-127"),
        ("temperature_empty", "empty"),
        ("temperature_too_hot", "plausible"),
    ],
)
def test_temperature_rejects(name, msg):
    with pytest.raises(SensorError, match=msg):
        parse_temperature_file(fx(name))


@pytest.mark.parametrize("c", [-20.5, 90.5])
def test_range_limits(c):
    with pytest.raises(SensorError):
        parse_temperature_file(str(int(c * 1000)))


def make_tree(root, devices, bulk=True, bulk_status="1"):
    if bulk:
        master = root / "w1_bus_master1"
        master.mkdir(parents=True)
        (master / "therm_bulk_read").write_text(bulk_status)
    for addr, (temperature, w1_slave) in devices.items():
        d = root / addr
        d.mkdir(parents=True)
        if temperature is not None:
            (d / "temperature").write_text(temperature)
        if w1_slave is not None:
            (d / "w1_slave").write_text(w1_slave)


def test_sysfs_bulk(tmp_path):
    make_tree(tmp_path, {
        "28-000000000001": ("21500\n", fx("w1_slave_ok")),
        "28-000000000002": ("85000\n", fx("w1_slave_ok")),
    })
    bus = SysfsBus(tmp_path)
    assert bus.addresses() == ["28-000000000001", "28-000000000002"]
    r = bus.read(["28-000000000001", "28-000000000002", "28-000000000009"])
    assert r["28-000000000001"].celsius == pytest.approx(21.5)  # from temperature, not w1_slave
    assert r["28-000000000002"].celsius is None and "85" in r["28-000000000002"].error
    assert r["28-000000000009"].celsius is None and "missing" in r["28-000000000009"].error
    # The trigger was written (our fake file just keeps what we wrote).
    assert (tmp_path / "w1_bus_master1" / "therm_bulk_read").read_text().strip() == "trigger"


class ScriptedBus(SysfsBus):
    """therm_bulk_read reports the given statuses in turn after the trigger."""

    def __init__(self, root, statuses):
        self.clock_s = 0.0
        self.sleeps = 0
        super().__init__(root, sleep=self._sleep_fn, clock=lambda: self.clock_s)
        self.statuses = list(statuses)

    def _sleep_fn(self, dt):
        self.sleeps += 1
        self.clock_s += dt

    def _bulk_status(self):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def test_sysfs_bulk_waits_for_conversion(tmp_path):
    make_tree(tmp_path, {"28-000000000001": ("21500\n", None)})
    bus = ScriptedBus(tmp_path, ["-1", "-1", "-1", "1"])
    r = bus.read(["28-000000000001"])
    assert bus.sleeps == 3
    assert r["28-000000000001"].celsius == pytest.approx(21.5)


def test_sysfs_bulk_timeout(tmp_path):
    make_tree(tmp_path, {"28-000000000001": ("", None)})
    bus = ScriptedBus(tmp_path, ["-1"])
    r = bus.read(["28-000000000001"])
    assert bus.clock_s >= 1.0  # gave up after the 1 s timeout
    assert r["28-000000000001"].celsius is None  # empty temperature file: still converting


def test_sysfs_fallback_to_w1_slave(tmp_path, caplog):
    make_tree(tmp_path, {
        "28-000000000001": (None, fx("w1_slave_ok")),
        "28-000000000002": (None, fx("w1_slave_crc_fail")),
    }, bulk=False)
    bus = SysfsBus(tmp_path)
    with caplog.at_level("WARNING"):
        r1 = bus.read(["28-000000000001", "28-000000000002"])
        r2 = bus.read(["28-000000000001"])
    assert r1["28-000000000001"].celsius == pytest.approx(23.125)
    assert r1["28-000000000002"].celsius is None and "CRC" in r1["28-000000000002"].error
    assert r2["28-000000000001"].celsius == pytest.approx(23.125)
    fallback_logs = [rec for rec in caplog.records if "falling back" in rec.message]
    assert len(fallback_logs) == 1  # logged once
