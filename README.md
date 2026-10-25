# heatsteal

Controller software for a two-person Peltier "heat theft" rig. One person wears a
cold headband and the other wears a warm vest. A Peltier module moves heat
between the two fluid loops. A Raspberry Pi 3B reads four DS18B20 probes and
drives three relays (Peltier, cold pump, hot pump) through a safety state
machine. It logs to SQLite for Grafana, and serves a web page and a CLI.

> **Safety first.** The loads are a Peltier module and two pumps attached to
> people. The software exists to guarantee two things: the Peltier never runs
> without both pumps, and any fault lands in a safe state. In a fault the
> Peltier is off and the pumps keep running. The 45 °C bimetallic cutout in
> series with the Peltier is a backstop, and the software never relies on it.

Contents: [Develop on a laptop](#develop-on-a-laptop) ·
[Wiring reference](#wiring-reference) · [Pi setup](#pi-setup) ·
[**Relay acceptance test**](#relay-acceptance-test) ·
[Labelling probes](#labelling-probes) · [Calibration](#calibration) ·
[Flow measurement](#flow-measurement) · [Grafana](#grafana) ·
[Read-only SD card](#read-only-sd-card) · [Operating](#operating) ·
[How it works](#how-it-works)

---

## Develop on a laptop

Everything that touches hardware sits behind an interface with a fake
implementation. With `hardware = "fake"`, a small thermal simulator stands in
for the rig.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest

cp config.example.toml dev.toml
# point storage.db_path somewhere writable, e.g. db_path = "dev.db"
.venv/bin/heatsteal-service --config dev.toml
# open http://localhost:8080, or:
.venv/bin/heatsteal --config dev.toml status
.venv/bin/heatsteal --config dev.toml start
.venv/bin/heatsteal --config dev.toml watch
```

To exercise the fault handling, set `[sim] fault` to `"runaway_hot"`,
`"dead_pump"` or `"stale_probe"`. Faults start `fault_after_s` seconds after
the Peltier first switches on.

---

## Wiring reference

**Relays:** a Waveshare RPi Relay Board (3 channel) on the GPIO header, with
its jumpers fitted. **The board is active-low**: a LOW pin energises the relay.

| Channel | BCM | Physical pin | Load |
|---|---|---|---|
| CH1 | 26 | 37 | Peltier (via buck converter) |
| CH2 | 20 | 38 | Cold-loop pump |
| CH3 | 21 | 40 | Hot-loop pump |

The channel-to-load mapping lives in `[relays]` in the config.

**Probes:** four waterproof DS18B20s on one 1-Wire bus on BCM 4 (physical
pin 7), with a 4.7 kΩ pull-up to 3.3 V. They are taped to the tubes just
outside the enclosure, then lagged.

| Name | Position |
|---|---|
| `cold_flow` | Cold loop, leaving the box towards the headband |
| `cold_return` | Cold loop, coming back from the headband |
| `hot_flow` | Hot loop, leaving the box towards the vest |
| `hot_return` | Hot loop, coming back from the vest |

**Backstop:** a 45 °C bimetallic cutout is wired in series with the Peltier.

The relays only switch on and off. There is no PWM and no polarity reversal.
The buck converter sets the Peltier current in hardware.

---

## Pi setup

Raspberry Pi OS Lite (64-bit), Python 3.11+.

### 1. Boot config

Append `deploy/boot-config.txt.snippet` to `/boot/firmware/config.txt` and
reboot:

```
dtoverlay=w1-gpio
gpio=20,21,26=op,dh
```

The second line makes the firmware drive the relay pins HIGH (relays off)
from the earliest moment. Without it, the pins' default pull-downs could
energise the active-low relays before the service starts.

### 2. Users, groups, install

```sh
sudo apt install python3-venv swig liblgpio-dev   # the last two only if pip has to build lgpio
sudo useradd --system --no-create-home --shell /usr/sbin/nologin heatsteal
sudo usermod -aG gpio heatsteal

sudo python3 -m venv /opt/heatsteal
sudo /opt/heatsteal/bin/pip install '/path/to/heatsteal[pi]'   # gpiozero + lgpio
sudo cp README.md /opt/heatsteal/

sudo mkdir -p /etc/heatsteal
sudo cp config.example.toml /etc/heatsteal/config.toml
sudo editor /etc/heatsteal/config.toml      # hardware = "pi", probe addresses, flow rates
```

### 3. Cap the database tmpfs at 64 MB

The database lives on tmpfs at `/run/heatsteal`, so the SD card is never
written during a session. `/run` is shared with the rest of the system, so
give heatsteal its own capped mount. Add this to `/etc/fstab`, using numeric
ids:

```sh
echo "tmpfs /run/heatsteal tmpfs size=64m,mode=0775,uid=$(id -u heatsteal),gid=$(id -g heatsteal),nosuid,nodev,noexec 0 0" | sudo tee -a /etc/fstab
sudo mkdir -p /run/heatsteal && sudo mount /run/heatsteal
```

At 1 Hz, 24 hours of data takes roughly 30 MB.

### 4. systemd unit

```sh
sudo cp deploy/heatsteal.service /etc/systemd/system/
sudo systemctl daemon-reload
```

**Don't enable or start it yet. Run the relay acceptance test first.**

The unit runs as `heatsteal:heatsteal` with `SupplementaryGroups=gpio` and
`UMask=0002`. It uses `Type=notify` with `WatchdogSec=10`, and
`Restart=on-failure` with `RestartSec=3`. `RuntimeDirectory=heatsteal`
(mode 0775, preserved across restarts) creates `/run/heatsteal`. An
`ExecStartPre` step gives the `heatsteal` group write access to
`therm_bulk_read`, so that all four probes convert at the same instant. If
that fails, the service logs a warning and falls back to reading each probe's
`w1_slave` file in turn.

---

## Relay acceptance test

**A human runs this before anything is plumbed.** Disconnect the Peltier and
the pumps. Listen for relay clicks and watch the board's LEDs; an LED lit
means that relay is energised.

The probes must be connected, though they can just hang in air: `start` is
refused unless all four are reading.

**All three relays must be off (no LED lit):**

1. **At power-on, before the service starts.** Leave the unit disabled. Power
   the Pi up from cold and watch the board from the moment power is applied
   until the login prompt.
2. **After `systemctl stop heatsteal`.** First
   `sudo systemctl start heatsteal`, then `heatsteal start`. Within about
   10 s you should hear CH2 and CH3 click (pumps), then CH1 (Peltier). Next,
   `sudo systemctl stop heatsteal`. All LEDs must go out.
3. **After `kill -9` of the service.** Start it again, run `heatsteal start`,
   wait for all three LEDs, then `sudo systemctl kill -s KILL heatsteal`. All
   LEDs must go out, and must stay out while systemd restarts the service
   (about 3 s later, in IDLE).
4. **After a reboot.** With the service running and all three relays on,
   `sudo reboot`. The relays must drop, and stay off through the whole boot,
   including after the service starts again (enable it for this one:
   `sudo systemctl enable heatsteal`).

Also check the mapping. In IDLE, `heatsteal pump cold 5` should click
**only CH2** for 5 s, and `heatsteal pump hot 5` **only CH3**.

**If any of these fails, stop and report it. Do not work around it in
software.** Tests 2 and 3 are the ones to watch. When the process exits,
whether cleanly (gpiozero closes its pins at exit) or by `kill -9`, the
kernel releases the GPIO lines. What a released pin does next depends on the
kernel's pinctrl driver, not on this code. Depending on kernel version and
settings, it either keeps driving its last level or reverts to an input with
its default pull-down. If it keeps driving its last level, a killed service
leaves the relays wherever they were, and test 3 fails with the Peltier still
on. If it reverts to an input, the board's input may float towards "on", and
test 2 or 3 can fail. Either result is a finding to report and fix in the
hardware or boot configuration, before going further.

---

## Labelling probes

With the service stopped:

```sh
sudo systemctl stop heatsteal
heatsteal sensors                  # lists every 28-xxxxxxxxxxxx on the bus with its reading
heatsteal sensors --watch          # refreshes each second; highlights the probe warming fastest
```

Hold one probe in your hand. It will be highlighted within a few seconds.
Write its address under the right name in `[sensors]`, then repeat for the
other three. If you have no config yet, add `--hardware pi`.

`sensors` and `calibrate` read the bus directly. They refuse to run while
the service is up, because the service owns the bus.

---

## Calibration

The ΔT values are only about 4–5 K, so a 0.5 °C probe error is a 10% power
error. Calibrate after labelling:

1. Put all four probes in the same container of water, close together.
   Stir, and keep it away from anything warm. Room-temperature water that
   has stood for an hour is ideal.
2. `sudo systemctl stop heatsteal`
3. `heatsteal calibrate --minutes 5`
4. Paste the printed `[offsets]` block into `/etc/heatsteal/config.toml`,
   replacing the old one, then start the service.

Each offset is the probe's mean difference from the group mean, and it is
*added* to the raw reading. The config rejects offsets larger than 5 °C,
because an offset that large means a bad probe or a bad calibration.

---

## Flow measurement

There is no flow sensor. Measure each loop by hand:

1. Put the loop's return line into a cup on a kitchen scale. Tare it.
2. `heatsteal pump cold 60` runs only the cold pump for 60 s. This only works
   in IDLE, and `heatsteal stop` cancels it.
3. Weigh the water. The flow in g/s is grams ÷ 60.
4. Repeat with `heatsteal pump hot 60`.
5. Enter the results in `[flow] cold` and `[flow] hot`. Expect about 1.1 g/s.

---

## Grafana

Everything is provisioned from files in `deploy/grafana/`. Nothing is
configured through the UI, which keeps it compatible with the read-only root
filesystem.

```sh
# Install Grafana (OSS, arm64) from Grafana's apt repo, then:
sudo grafana-cli plugins install frser-sqlite-datasource
sudo usermod -aG heatsteal grafana          # WAL readers need to write the -shm file

# merge deploy/grafana/grafana.ini.snippet into /etc/grafana/grafana.ini
sudo cp deploy/grafana/provisioning/datasources/heatsteal.yaml /etc/grafana/provisioning/datasources/
sudo cp deploy/grafana/provisioning/dashboards/heatsteal.yaml  /etc/grafana/provisioning/dashboards/
sudo mkdir -p /etc/grafana/dashboards/heatsteal
sudo cp deploy/grafana/dashboards/heatsteal.json /etc/grafana/dashboards/heatsteal/
sudo systemctl enable --now grafana-server
```

What the files set up:

* **The datasource** points at `/run/heatsteal/heatsteal.db`. The plugin
  opens it read-only (`query_only`). The database is in WAL mode, so readers
  also need write access to the directory to create `heatsteal.db-shm`. That
  is why `grafana` is in the `heatsteal` group, the directory is 0775, and
  the service runs with `UMask=0002`.
* **The dashboard** (uid `heatsteal`) has three panels: temperatures (panel
  1), watts per loop (panel 2), and kJ stolen this session (panel 3, a stat).
  It also annotates state changes and faults from `events`. The web page
  embeds panels 1–3 through `/d-solo/heatsteal/heatsteal?panelId=N`.
* **The queries** follow the plugin's documented conventions. `ts` is
  Unix-epoch seconds. `$__from` and `$__to` are milliseconds, hence the
  `/ 1000.0`. `$__unixEpochGroupSeconds(ts, 5)` buckets the data into 5 s
  groups. Time-series queries return long format (time, label, value) sorted
  ascending, which the plugin converts to wide format. `tests/test_grafana.py`
  runs every dashboard query against the real schema.
* **`grafana.ini`** sets `allow_embedding = true` and enables anonymous
  Viewer access, so the iframes render without a login.

**Check while the service is writing.** During a session, open the dashboard
and confirm that the panels update without "attempt to write a readonly
database" errors. If you see that error, the `grafana` user can't write the
`-shm` file: check `id grafana`, the directory mode, and restart
grafana-server after changing its groups.

The page works without Grafana. Only the history iframes go blank.

---

## Read-only SD card

Develop with a writable root. Once the code and config have settled:

1. Finish all installs first: the venv, Grafana plus the plugin,
   provisioning files, fstab, and `/etc/heatsteal/config.toml`.
2. `sudo raspi-config` → Performance Options → Overlay File System →
   enable it (and make the boot partition read-only when asked). Reboot.

After that, every write to the root filesystem goes to RAM and is lost at
reboot. That is fine:

* The service only writes to `/run/heatsteal`, which is tmpfs anyway.
* Grafana's own state in `/var/lib/grafana` becomes disposable, because the
  datasource and dashboard come from provisioning files.
* To keep session data, set `[storage] archive_dir` to a mounted USB stick.
  At the end of each session the service runs one `VACUUM INTO` there.

**Changing the config means disabling the overlay first**: raspi-config →
disable the Overlay File System → reboot → edit → re-enable → reboot. An edit
made while the overlay is on looks like it worked, then vanishes at the next
reboot.

---

## Operating

### Web page

Open `http://<pi>:8080/`. It shows:

* watts stolen in huge type, with kJ stolen this session beneath it
* the state as a coloured badge
* the four temperatures, with stale probes greyed out
* three relay LEDs
* Start, Stop and Reset buttons, each enabled only when it's valid
* fault and warning text, prominently
* Grafana history below all of that

If `control_token` is set, use "Set control token" on the page. It is stored
in that browser's localStorage. Set a token whenever the rig is on shared
wifi.

### CLI

```
heatsteal status            # one-shot status
heatsteal watch             # refreshing status
heatsteal start | stop | reset
heatsteal pump cold 60      # flow measurement (IDLE only)
heatsteal events            # recent events
heatsteal sensors [--watch] # direct bus access; service must be stopped
heatsteal calibrate --minutes 5
```

The CLI reads the port from `/etc/heatsteal/config.toml` (or `--config`).
You can override the address with `--url` or `$HEATSTEAL_URL`, and supply a
token with `--token` or `$HEATSTEAL_TOKEN`.

### API

| Method | Path | |
|---|---|---|
| GET | `/api/state` | state, time in state, per-probe °C and staleness, relays, `p_cold_w`, `p_hot_w`, `kj_cold`, `kj_hot`, `last_fault`, `warnings`, `valid_commands`, `pump_job` |
| POST | `/api/command` | `{"action": "start" \| "stop" \| "reset"}` or `{"action": "pump", "loop": "cold", "seconds": 60}`. Returns `{ok, state}`, or 409 `{ok: false, state, reason}`. |
| GET | `/api/events?limit=50` | recent events, newest first |
| GET | `/api/ui` | Grafana URL and whether auth is on (used by the page) |

With `control_token` set, every POST needs `Authorization: Bearer <token>`.
GETs stay open.

---

## How it works

### States

| State | Peltier | Cold pump | Hot pump | Leaves when |
|---|---|---|---|---|
| `IDLE` | off | off | off | `start` → PRIMING (all probes must be reading) |
| `PRIMING` | off | on | on | ≥ `prime_s` and all probes fresh → RUNNING; `stop` → COOLDOWN |
| `RUNNING` | on | on | on | `stop` or `max_run_minutes` → COOLDOWN |
| `COOLDOWN` | off | on | on | `cooldown_s` → IDLE |
| `FAULT` | off | **on** | **on** | latched until `reset` → COOLDOWN |

The service always starts in IDLE. A command that isn't valid in the current
state is rejected with a reason, and the rejection is logged as an event.
The pumps stay on in FAULT and COOLDOWN because stopping the flow while the
module's faces are at different temperatures cracks Peltier modules.

`pump <loop> <seconds>` (at most 600 s) runs one pump while the state stays
IDLE. `stop` cancels it, and `start` is refused while it runs.

### Faults

A fault takes PRIMING, RUNNING or COOLDOWN to FAULT. The possible faults are:

* a probe is stale (no good reading for `sensor_stale_s`)
* the sampler itself stops publishing
* `hot_flow` ≥ `hot_max_c`
* `cold_flow` ≤ `cold_min_c`

The **dead-pump heuristics** only run in RUNNING, after `warmup_s`. Each one
warns by default, and you can switch each to `"fault"` once it's tuned on real
hardware:

* `hot_flow` rising faster than `hot_rise_max_k_per_min`
* `cold_flow` falling faster than `cold_fall_max_k_per_min` (both are
  least-squares slopes over the last 60 s)
* either loop's ΔT above `loop_dt_max_k`

The **cutout heuristic** (always a warning) covers the case where the Peltier
is commanded on but a loop's ΔT stays below `min_loop_dt_k` for
`min_loop_dt_hold_s`. That usually means the 45 °C cutout has opened, which
in turn usually means a hot-side flow problem.

### Defence in depth

* The `Relays` class enforces "Peltier only while both pumps are on" by
  itself. It switches pumps before the Peltier when turning on, and the
  Peltier first when turning anything off. A bad request forces the Peltier
  off and raises, and the service treats that as a crash.
* GPIO: `DigitalOutputDevice(pin, active_high=False, initial_value=False)`
  expresses the active-low wiring, and the pins come up driven HIGH.
* Only the control thread holds the relay object. The web server only reads
  status and queues commands. Uvicorn runs exactly one worker.
* The control loop sends `WATCHDOG=1` every tick. If it hangs, systemd kills
  the process.
* On any unhandled exception the service calls `all_off()`, logs, and exits
  non-zero. systemd restarts it, and it comes back in IDLE. SIGTERM, SIGINT
  and SIGHUP also call `all_off()`.
* Control timing uses the monotonic clock, so an NTP jump on a Pi without an
  RTC can't stretch or skip timers. Stored timestamps use the wall clock.

### Physics

* `P_cold = m_dot_cold × c × (cold_return − cold_flow)` is the heat stolen
  from the headband wearer.
* `P_hot = m_dot_hot × c × (hot_flow − hot_return)` is the heat delivered to
  the vest wearer.

Calibration offsets are applied first. Session energy is the trapezoidal
integral of each power figure, reset at session start. A gap in readings is
not integrated across.

### Storage

SQLite at `/run/heatsteal/heatsteal.db`, with `journal_mode=WAL` and
`synchronous=OFF`. Rows are committed in batches every 5 s, and events are
committed promptly. The tables are `sessions`, `readings` (long format, one
row per probe per sample; missing readings are not written), `power` (with
cumulative session joules), `relays` (written only on a change) and
`events`. Rows older than `retention_hours` are deleted once a minute.

### Layout

```
src/heatsteal/
  config.py      TOML load + strict validation
  model.py       Snapshot, State, Command, Event
  physics.py     power + trapezoidal energy
  sensors.py     SysfsBus (bulk read, w1_slave fallback), FakeBus, validation
  relays.py      Relays invariant; GpioRelays; FakeRelays
  sim.py         lumped thermal model (incl. the 45 °C cutout) + fault injection
  controller.py  pure state machine: step(cfg, ctx, snapshot, command, now)
  storage.py     SQLite writer, retention, archive
  service.py     sampler, control thread, watchdog, signals, uvicorn
  web.py         FastAPI
  cli.py         `heatsteal` command (never imports relays.py)
  static/index.html
deploy/          systemd unit, boot snippet, Grafana provisioning
tests/           pytest; no hardware needed
```
