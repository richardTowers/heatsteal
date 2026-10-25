# heatsteal: notes for Claude

A Raspberry Pi service controlling a Peltier rig attached to two people.
README.md covers wiring, setup, the state table, physics and storage. Read
it before changing behaviour.

## Real hardware

- Never switch relays on real hardware yourself. Give the human the command
  to run and say what they should see and hear (which LEDs, which clicks).
- If the relay acceptance test in the README fails, report it. Don't work
  around it in software.

## Invariants: keep these when changing code

- `relays.py` enforces "Peltier only while both pumps are on" by itself,
  independent of the state machine. It switches pumps before the Peltier
  when turning on, and the Peltier first when turning anything off.
- Only the control thread in `service.py` holds the relay object. The web
  server only reads status and queues commands.
- Uvicorn runs exactly one worker. More workers would mean more processes
  fighting over the GPIO pins.
- `cli.py` never imports `relays.py` or gpiozero (`tests/test_cli.py`
  checks this).
- FAULT and COOLDOWN keep the pumps on and the Peltier off. FAULT stays
  latched until `reset`.
- On any unhandled exception: relays all off, log it, exit non-zero.
- Control timing uses the monotonic clock. Stored timestamps use the wall
  clock.

## Conventions

- `controller.py` is pure: `step(cfg, ctx, snapshot, command, now)`, with
  no I/O and an injected clock. Test new behaviour there.
- Every threshold comes from config. Add new keys to the dataclass in
  `config.py`, validate them there, and document them in
  `config.example.toml`. Unknown keys are an error.
- Keep the dependencies short (this runs on a Pi 3). Use the stdlib unless
  there's a strong reason not to. gpiozero and lgpio stay in the `pi` extra.
- Check kernel (w1_therm, GPIO) and Grafana-plugin behaviour against their
  docs or source before relying on it, rather than guessing.
- The page embeds dashboard panels by ID. If you change a panel ID, change
  `static/index.html` too (`tests/test_grafana.py` catches a mismatch).

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                   # no hardware needed
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy   # same as CI
.venv/bin/heatsteal-service --config dev.toml      # hardware = "fake" runs the simulator
```

For `dev.toml`, copy `config.example.toml` and point `storage.db_path`
somewhere writable. To test fault handling, set `[sim] fault`.
