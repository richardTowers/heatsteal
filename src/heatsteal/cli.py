"""The ``heatsteal`` command.

Most subcommands are a thin HTTP client for the running service. ``sensors``
and ``calibrate`` read the 1-Wire bus directly and refuse to run while the
service is up. This module must never import the GPIO code (relays.py).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque

from . import config as config_mod
from .config import PROBES

DEFAULT_PORT = 8080


class ApiError(Exception):
    pass


# -- HTTP -------------------------------------------------------------------


def _default_url(config_path: str) -> str:
    if os.environ.get("HEATSTEAL_URL"):
        return os.environ["HEATSTEAL_URL"]
    port = DEFAULT_PORT
    try:
        port = config_mod.load(config_path).web.port
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


def api(base: str, path: str, body: dict | None = None, token: str = "", timeout: float = 10.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            payload = json.load(e)
        except Exception:
            payload = {}
        reason = payload.get("reason") or payload.get("detail") or e.reason
        raise ApiError(f"HTTP {e.code}: {reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"cannot reach heatsteal at {base}: {getattr(e, 'reason', e)}") from None


# -- status -----------------------------------------------------------------


def _f(v, digits=1, unit=""):
    return "–" if v is None else f"{v:.{digits}f}{unit}"


def format_status(s: dict) -> str:
    lines = []
    lines.append(f"State      {s['state']}  ({s['time_in_state_s']:.0f} s)")
    lines.append(f"Stolen     {_f(s['p_cold_w'], 1, ' W'):>9}   {s['kj_cold']:.2f} kJ this session")
    lines.append(f"Delivered  {_f(s['p_hot_w'], 1, ' W'):>9}   {s['kj_hot']:.2f} kJ this session")
    lines.append("")
    for name, p in s["probes"].items():
        flag = "  STALE" if p["stale"] else ""
        err = f"  ({p['error']})" if p.get("error") else ""
        lines.append(f"  {name:<12} {_f(p['celsius'], 2, ' °C'):>10}{flag}{err}")
    r = s["relays"]
    onoff = lambda b: "ON " if b else "off"  # noqa: E731
    lines.append("")
    lines.append(f"Relays     peltier {onoff(r['peltier'])}  cold_pump {onoff(r['cold_pump'])}  hot_pump {onoff(r['hot_pump'])}")
    if s.get("pump_job"):
        lines.append(f"Pump job   {s['pump_job']['loop']} loop, {s['pump_job']['remaining_s']:.0f} s left")
    if s["state"] == "FAULT":
        lines.append(f"FAULT      {s['last_fault']}")
    for w in s.get("warnings", []):
        lines.append(f"WARNING    {w}")
    return "\n".join(lines)


def cmd_status(args) -> int:
    print(format_status(api(args.url, "/api/state")))
    return 0


def cmd_watch(args) -> int:
    try:
        while True:
            try:
                text = format_status(api(args.url, "/api/state", timeout=3))
            except ApiError as e:
                text = f"OFFLINE: {e}"
            sys.stdout.write("\x1b[H\x1b[2J" + time.strftime("%H:%M:%S") + "\n\n" + text + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _command(args, body: dict) -> int:
    r = api(args.url, "/api/command", body, token=args.token)
    print(f"ok, state is {r['state']}")
    return 0


def cmd_simple(args) -> int:
    return _command(args, {"action": args.action})


def cmd_pump(args) -> int:
    return _command(args, {"action": "pump", "loop": args.loop, "seconds": args.seconds})


def cmd_events(args) -> int:
    for ev in reversed(api(args.url, f"/api/events?limit={args.limit}")):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["ts"]))
        print(f"{ts}  {ev['kind']:<7} {ev['detail']}")
    return 0


# -- direct bus access --------------------------------------------------------


def service_running(url: str) -> str | None:
    """Return why we think the service is running, or None."""
    try:
        api(url, "/api/state", timeout=2)
        return f"the API answers at {url}"
    except ApiError:
        pass
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "is-active", "--quiet", "heatsteal"], check=False)
        if r.returncode == 0:
            return "systemctl reports heatsteal is active"
    return None


def _open_bus(cfg):
    from .sensors import FakeBus, SysfsBus

    if cfg.hardware == "pi":
        return SysfsBus()
    from .model import RelayOutputs
    from .sim import ThermalSim

    sim = ThermalSim(cfg.sim, lambda: RelayOutputs(), (cfg.flow.cold, cfg.flow.hot), cfg.flow.specific_heat)
    return FakeBus(sim, cfg.address_to_probe())


def _refuse_if_running(args) -> bool:
    why = service_running(args.url)
    if why:
        print(f"refusing: the service is running ({why}). Stop it first: sudo systemctl stop heatsteal",
              file=sys.stderr)
        return True
    return False


def _load_cfg_for_bus(args):
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        if args.hardware:
            # No usable config yet (e.g. first time labelling probes): a minimal one will do.
            raw = {"hardware": args.hardware,
                   "sensors": {p: f"28-00000000000{i + 1}" for i, p in enumerate(PROBES)}}
            return config_mod.from_dict(raw)
        raise SystemExit(f"{e}\n(pass --hardware pi to scan the bus without a config)")
    if args.hardware:
        cfg = dataclasses.replace(cfg, hardware=args.hardware)
    return cfg


def cmd_sensors(args) -> int:
    if _refuse_if_running(args):
        return 1
    cfg = _load_cfg_for_bus(args)
    bus = _open_bus(cfg)
    names = cfg.address_to_probe()
    history: dict[str, deque] = {}

    def once() -> list[str]:
        addrs = bus.addresses()
        if not addrs:
            return ["no DS18B20 probes found on the bus (is dtoverlay=w1-gpio set?)"]
        readings = bus.read(addrs)
        now = time.monotonic()
        rates = {}
        for a, r in readings.items():
            h = history.setdefault(a, deque(maxlen=10))
            if r.celsius is not None:
                h.append((now, r.celsius))
            if len(h) >= 2 and h[-1][0] - h[0][0] > 0:
                rates[a] = (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0]) * 60
        warmest = max(rates, key=rates.get) if rates else None
        if warmest is not None and rates[warmest] <= 0.2:
            warmest = None  # nothing is obviously warming
        out = [f"{'address':<17} {'name':<12} {'°C':>8}  {'K/min':>6}"]
        for a in addrs:
            r = readings[a]
            val = f"{r.celsius:8.3f}" if r.celsius is not None else f"{'–':>8}"
            rate = f"{rates[a]:+6.1f}" if a in rates else f"{'':>6}"
            line = f"{a:<17} {names.get(a, '(unmapped)'):<12} {val}  {rate}"
            if r.error:
                line += f"  {r.error}"
            if args.watch and a == warmest:
                line = f"\x1b[7m{line}  <- warming fastest\x1b[0m"
            out.append(line)
        return out

    if not args.watch:
        print("\n".join(once()))
        return 0
    try:
        while True:
            t0 = time.monotonic()
            lines = once()
            sys.stdout.write("\x1b[H\x1b[2J" + "Hold a probe in your hand to find it. Ctrl-C to stop.\n\n"
                             + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        return 0


def compute_offsets(samples: list[dict[str, float]]) -> dict[str, float]:
    """Each probe's mean offset from the group mean, for complete samples only.

    The offset is what to *add* to that probe's raw reading.
    """
    complete = [s for s in samples if all(s.get(p) is not None for p in PROBES)]
    if not complete:
        raise ValueError("no sample had all four probes")
    diffs = {p: [] for p in PROBES}
    for s in complete:
        mean = sum(s[p] for p in PROBES) / len(PROBES)
        for p in PROBES:
            diffs[p].append(mean - s[p])
    return {p: statistics.fmean(d) for p, d in diffs.items()}


def format_offsets(offsets: dict[str, float]) -> str:
    lines = ["[offsets]                  # °C, from `heatsteal calibrate`"]
    for p in PROBES:
        lines.append(f"{p} = {offsets[p]:.3f}")
    return "\n".join(lines)


def cmd_calibrate(args) -> int:
    if _refuse_if_running(args):
        return 1
    cfg = _load_cfg_for_bus(args)
    bus = _open_bus(cfg)
    addrs = [cfg.sensors[p] for p in PROBES]
    end = time.monotonic() + args.minutes * 60
    samples: list[dict[str, float]] = []
    print(f"Recording for {args.minutes:g} min. All four probes must be in the same container of water,", file=sys.stderr)
    print("stirred, away from anything warm. Ctrl-C to stop early.", file=sys.stderr)
    try:
        while time.monotonic() < end:
            t0 = time.monotonic()
            readings = bus.read(addrs)
            s = {p: readings[cfg.sensors[p]].celsius for p in PROBES}
            samples.append(s)
            shown = "  ".join(f"{p}={v:.3f}" if v is not None else f"{p}=–" for p, v in s.items())
            print(f"\r{len(samples):4d}  {shown}", end="", file=sys.stderr, flush=True)
            time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    print(file=sys.stderr)
    complete = [s for s in samples if all(v is not None for v in s.values())]
    if len(complete) < 10:
        print(f"only {len(complete)} complete samples; need at least 10", file=sys.stderr)
        return 1
    offsets = compute_offsets(complete)
    spread = max(max(s.values()) - min(s.values()) for s in complete)
    print(f"{len(complete)} complete samples; worst raw spread {spread:.3f} °C", file=sys.stderr)
    print(format_offsets(offsets))
    return 0


# -- main -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="heatsteal", description="Control and inspect the heatsteal rig")
    ap.add_argument("--config", default=os.environ.get("HEATSTEAL_CONFIG", str(config_mod.DEFAULT_PATH)))
    ap.add_argument("--url", help="service URL (default: $HEATSTEAL_URL or http://127.0.0.1:<port from config>)")
    ap.add_argument("--token", default=os.environ.get("HEATSTEAL_TOKEN", ""),
                    help="control token for commands (default: $HEATSTEAL_TOKEN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="one-shot status").set_defaults(func=cmd_status)
    w = sub.add_parser("watch", help="refreshing status")
    w.add_argument("--interval", type=float, default=1.0)
    w.set_defaults(func=cmd_watch)
    for action in ("start", "stop", "reset"):
        sub.add_parser(action, help=f"send {action}").set_defaults(func=cmd_simple, action=action)
    p = sub.add_parser("pump", help="run one pump for a fixed time (flow measurement; IDLE only)")
    p.add_argument("loop", choices=("cold", "hot"))
    p.add_argument("seconds", type=float)
    p.set_defaults(func=cmd_pump)
    e = sub.add_parser("events", help="recent events")
    e.add_argument("--limit", type=int, default=30)
    e.set_defaults(func=cmd_events)

    s = sub.add_parser("sensors", help="list probes on the bus (service must be stopped)")
    s.add_argument("--watch", action="store_true", help="refresh every second; highlight the probe warming fastest")
    s.add_argument("--hardware", choices=("pi", "fake"), help="override config's hardware setting")
    s.set_defaults(func=cmd_sensors)
    c = sub.add_parser("calibrate", help="all probes in one container of water; prints an [offsets] block")
    c.add_argument("--minutes", type=float, default=5.0)
    c.add_argument("--hardware", choices=("pi", "fake"), help=argparse.SUPPRESS)
    c.set_defaults(func=cmd_calibrate)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url:
        args.url = _default_url(args.config)
    try:
        return args.func(args)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
