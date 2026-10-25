"""Render the wiring diagrams in this directory.

    python docs/wiring/build.py            # writes power.svg, control.svg, harness-sensors.svg
    python docs/wiring/build.py --check    # fails if a committed schematic SVG is stale

power.svg and control.svg are drawn with Schemdraw (pure Python, so the
output is reproducible). harness-sensors.svg is drawn by WireViz from
harness-sensors.yml and needs Graphviz (`dot`) installed. Its layout depends
on the Graphviz version, so CI doesn't check it for staleness.

These diagrams document the hardware as built. Change them only to match a
change in the hardware.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import schemdraw
import schemdraw.elements as elm

HERE = Path(__file__).resolve().parent

FONT = "DejaVu Sans"
NOTE = 9


def _drawing() -> schemdraw.Drawing:
    d = schemdraw.Drawing(show=False)
    d.config(fontsize=11, font=FONT, unit=2.5, lw=1.4)
    return d


def _pump_branch(d: schemdraw.Drawing, x: float, top: float, bottom: float, relay: str, pump: str, diode: str) -> None:
    """Rail → relay contact (COM→NO) → pump, with a flyback diode across the pump."""
    d.add(elm.Dot().at((x, top)))
    d.add(elm.Switch().at((x, top)).down().length(3).label(f"{relay}\nCOM→NO", loc="bottom", ofst=0.3))
    d.add(elm.Line().down().length(1))
    node_top = d.here
    d.add(elm.Dot())
    d.add(elm.Motor().down().length(3).label(f"{pump}\n12 V peristaltic", loc="top", ofst=0.3))
    node_bot = d.here
    d.add(elm.Dot())
    d.add(elm.Line().to((x, bottom)))
    d.add(elm.Dot())
    # Flyback diode, cathode to pump +
    d.add(elm.Line().at(node_top).right().length(2))
    d.add(
        elm.Diode()
        .at((node_bot[0] + 2, node_bot[1]))
        .up()
        .to((node_top[0] + 2, node_top[1]))
        .label(f"{diode}\n1N4001", loc="bottom", ofst=0.3, halign="left")
    )
    d.add(elm.Line().at(node_bot).right().length(2))


def draw_power() -> schemdraw.Drawing:
    d = _drawing()
    top, bottom = 0.0, -12.0
    x_end = 28.0

    # -- source -------------------------------------------------------------
    bank = d.add(
        elm.Ic(pins=[elm.IcPin(name="USB-C1", side="right")], size=(3.6, 2.2), pinspacing=1)
        .theta(0)
        .at((-15.5, -6))
        .label("Baseus EnerGeek GP12\nUSB-C PD power bank", loc="top", fontsize=10)
    )
    bank_x, bank_y = bank.absanchors["USB-C1"]
    trig = d.add(
        elm.Ic(
            pins=[
                elm.IcPin(name="12V+", side="right", slot="2/2"),
                elm.IcPin(name="12V−", side="right", slot="1/2"),
                elm.IcPin(name="USB-C", side="left"),
            ],
            size=(3.4, 3),
            pinspacing=1.5,
        )
        .theta(0)
        .anchor("USB-C")
        .at((bank_x + 2.5, bank_y))
        .label("USB-C PD trigger cable\n12 V / 3 A contract", loc="top", fontsize=10)
    )
    d.add(elm.Line().at(bank.absanchors["USB-C1"]).to(trig.absanchors["USB-C"]))

    # -- rails ----------------------------------------------------------------
    d.add(elm.Line().at(trig.absanchors["12V+"]).right().length(1.2))
    d.add(elm.Line().up().to((d.here[0], top)))
    d.add(
        elm.Line().right().to((x_end, top)).color("#c0392b").label("12 V+ rail  (Wago 221-415)", loc="top", ofst=0.15)
    )
    d.add(elm.Line().at(trig.absanchors["12V−"]).right().length(1.2))
    d.add(elm.Line().down().to((d.here[0], bottom)))
    d.add(
        elm.Line()
        .right()
        .to((x_end, bottom))
        .color("#2c3e50")
        .label("12 V− rail  (Wago 221-415)", loc="bottom", ofst=0.15)
    )

    # -- Peltier branch -------------------------------------------------------
    x = 2.0
    d.add(elm.Dot().at((x, top)))
    d.add(elm.Switch().at((x, top)).down().length(3).label("K1  CH1\nCOM→NO", loc="bottom", ofst=0.3))
    d.add(elm.Line().down().length(1.5))
    buck = d.add(
        elm.Ic(
            pins=[
                elm.IcPin(name="IN+", side="left", slot="2/2"),
                elm.IcPin(name="IN−", side="left", slot="1/2"),
                elm.IcPin(name="OUT+", side="right", slot="2/2"),
                elm.IcPin(name="OUT−", side="right", slot="1/2"),
            ],
            size=(4.2, 3.2),
            pinspacing=1.6,
        )
        .theta(0)
        .anchor("IN+")
        .at(d.here)
        .label("U1  XL4015 buck", loc="top", fontsize=10)
        .label("constant voltage\nset for ≈1.5 A", loc="center", fontsize=NOTE)
    )
    d.add(elm.Line().at(buck.absanchors["IN−"]).to((x, bottom)))
    d.add(elm.Dot())

    out_p = buck.absanchors["OUT+"]
    out_n = buck.absanchors["OUT−"]
    d.add(elm.Line().at(out_p).right().length(1))
    d.add(
        elm.Switch(nc=True)
        .right()
        .length(3.5)
        .label("TS1  KSD9700 45 °C\nnormally closed", loc="top", ofst=0.25)
        .label("bolted to hot block", loc="bottom", ofst=0.25, fontsize=NOTE)
    )
    d.add(elm.Line().right().length(1))
    tec = d.add(
        elm.Ic(
            pins=[elm.IcPin(name="+", side="left", slot="2/2"), elm.IcPin(name="−", side="left", slot="1/2")],
            size=(3, 3.2),
            pinspacing=1.6,
        )
        .theta(0)
        .anchor("+")
        .at(d.here)
        .label("TEC1  Peltier", loc="top", fontsize=10)
        .label("≈1.5 A", loc="center", fontsize=NOTE)
    )
    d.add(elm.Line().at(out_n).to(tec.absanchors["−"]))

    # -- pumps ----------------------------------------------------------------
    _pump_branch(d, 19.8, top, bottom, "K2  CH2", "M1  cold pump", "D1")
    _pump_branch(d, 28.0, top, bottom, "K3  CH3", "M2  hot pump", "D2")

    # -- notes ----------------------------------------------------------------
    notes = (
        "Notes\n"
        "• Relay contacts only. K1–K3 are the Waveshare RPi Relay Board channels; their coils are\n"
        "   driven from the Pi header 5 V and are isolated from these contacts (see control.svg).\n"
        "• No common ground between the Pi and the 12 V side. NC contacts unused.\n"
        "• Overcurrent limit: the power bank's 12 V / 3 A PD contract. Expected draw ≈2.1 A.\n"
        "• TS1 is a hardware backstop; the software never relies on it."
    )
    d.add(elm.Label().at((-17, bottom - 1.2)).label(notes, halign="left", valign="top", fontsize=NOTE))
    return d


RED, YELLOW, BLACK = "#c0392b", "#b7950b", "#000000"  # probe lead colours (yellow darkened to read on white)


def draw_control() -> schemdraw.Drawing:
    d = _drawing()
    sp = 1.2

    # Pi header pins used, top to bottom. slot k/N counts up from the bottom.
    rows = [
        ("1", "3V3"),
        ("7", "GPIO4 / 1-Wire"),
        ("9", "GND"),
        None,
        ("2,4", "5V"),
        ("37", "GPIO26"),
        ("38", "GPIO20"),
        ("40", "GPIO21"),
        ("6", "GND "),
    ]
    n = len(rows)
    pins = [
        elm.IcPin(name=f"{name}  ({num})", side="right", slot=f"{n - i}/{n}", anchorname=f"p{num}", lblsize=10)
        for i, r in enumerate(rows)
        if r
        for num, name in [r]
    ]
    pi = d.add(
        elm.Ic(pins=pins, size=(4.2, (n + 1) * sp), pinspacing=sp)
        .theta(0)
        .at((0, 0))
        .label("Raspberry Pi 3B\n40-pin header", loc="top", fontsize=10)
        .label(
            "Powered by micro-USB 5 V from\npower bank USB-A2, separate\nfrom the 12 V side",
            loc="bottom",
            fontsize=NOTE,
        )
    )
    a = pi.absanchors

    # -- 1-Wire bus -----------------------------------------------------------
    junction_x = a["p1"][0] + 5
    bus_end = junction_x + 5 + 3 * 3.6 + 1.6
    buses = {"VDD": ("p1", RED), "DQ": ("p7", YELLOW), "GND": ("p9", BLACK)}
    for _name, (pin, colour) in buses.items():
        d.add(elm.Line().at(a[pin]).to((bus_end, a[pin][1])).color(colour))
    d.add(elm.Label().at((a["p1"][0] + 0.4, a["p1"][1] + 0.35)).label("red", halign="left", fontsize=NOTE, color=RED))
    d.add(
        elm.Label()
        .at((a["p7"][0] + 0.4, a["p7"][1] + 0.35))
        .label("yellow", halign="left", fontsize=NOTE, color=YELLOW)
    )
    d.add(
        elm.Label().at((a["p9"][0] + 0.4, a["p9"][1] + 0.35)).label("black", halign="left", fontsize=NOTE, color=BLACK)
    )

    # Pull-up at the shared junction (screw terminals)
    rx = junction_x + 1.5
    d.add(elm.Dot().at((rx, a["p1"][1])))
    d.add(
        elm.Resistor()
        .at((rx, a["p1"][1]))
        .to((rx, a["p7"][1]))
        .label("R1\n4.7 kΩ", loc="bottom", ofst=0.2, fontsize=NOTE)
    )
    d.add(elm.Dot().at((rx, a["p7"][1])))
    box_top = a["p1"][1] + 1.0
    box_bot = a["p9"][1] - 0.6
    d.add(elm.Line().at((junction_x, box_top)).to((junction_x + 3.4, box_top)).linestyle("--").color("#888"))
    d.add(elm.Line().at((junction_x, box_bot)).to((junction_x + 3.4, box_bot)).linestyle("--").color("#888"))
    d.add(elm.Line().at((junction_x, box_top)).to((junction_x, box_bot)).linestyle("--").color("#888"))
    d.add(elm.Line().at((junction_x + 3.4, box_top)).to((junction_x + 3.4, box_bot)).linestyle("--").color("#888"))
    d.add(
        elm.Label()
        .at((junction_x + 1.7, box_top + 0.1))
        .label("TB1 screw terminals\n(shared junction)", valign="bottom", fontsize=NOTE)
    )

    # Four DS18B20 probes in parallel, hanging above the buses.
    names = ["cold_flow", "cold_return", "hot_flow", "hot_return"]
    for i, name in enumerate(names):
        x0 = junction_x + 5 + i * 3.6
        probe = d.add(
            elm.Ic(
                pins=[
                    elm.IcPin(name="VDD", side="bottom", slot="1/3", anchorname="VDD", lblsize=NOTE),
                    elm.IcPin(name="DQ", side="bottom", slot="2/3", anchorname="DQ", lblsize=NOTE),
                    elm.IcPin(name="GND", side="bottom", slot="3/3", anchorname="GND", lblsize=NOTE),
                ],
                size=(2.7, 1.4),
                pinspacing=0.8,
            )
            .theta(0)
            .anchor("VDD")
            .at((x0, a["p1"][1] + 1.6))
            .label(f"U{i + 1}  DS18B20\n{name}", loc="top", fontsize=NOTE)
        )
        for pin, (bus_pin, colour) in buses.items():
            px = probe.absanchors[pin][0]
            d.add(elm.Line().at(probe.absanchors[pin]).to((px, a[bus_pin][1])).color(colour))
            d.add(elm.Dot().at((px, a[bus_pin][1])).color(colour))

    # -- relay board ----------------------------------------------------------
    rb_x = a["p2,4"][0] + 8.5
    relay_rows = [("p2,4", "VCC (5 V)"), ("p37", "CH1  IN"), ("p38", "CH2  IN"), ("p40", "CH3  IN"), ("p6", "GND")]
    rpins = [
        elm.IcPin(name=label, side="left", slot=f"{5 - i}/5", anchorname=f"in{i}", lblsize=10)
        for i, (_, label) in enumerate(relay_rows)
    ]
    rpins += [
        elm.IcPin(name=f"K{k}  COM / NO", side="right", slot=f"{4 - k}/3", anchorname=f"k{k}", lblsize=10)
        for k in (1, 2, 3)
    ]
    rb = d.add(
        elm.Ic(pins=rpins, size=(6, 6 * sp), pinspacing=sp)
        .theta(0)
        .anchor("in0")
        .at((rb_x, a["p2,4"][1]))
        .label("Waveshare RPi Relay Board (3 ch)\nfitted on the header, jumpers shorted", loc="top", fontsize=10)
        .label(
            "Active-low: pin LOW energises the relay.\nContacts are isolated from the coil side.",
            loc="bottom",
            fontsize=NOTE,
        )
    )
    for i, (pin, _) in enumerate(relay_rows):
        d.add(elm.Line().at(a[pin]).to(rb.absanchors[f"in{i}"]))
    loads = {1: "Peltier (via buck)", 2: "cold pump", 3: "hot pump"}
    for k in (1, 2, 3):
        d.add(elm.Line().at(rb.absanchors[f"k{k}"]).right().length(1.5))
        d.add(
            elm.Label()
            .at((d.here[0] + 0.2, d.here[1]))
            .label(f"→ {loads[k]}, see power.svg", halign="left", fontsize=NOTE)
        )
    d.add(
        elm.Label()
        .at((a["p6"][0] - 4.2, a["p6"][1] - 3.2))
        .label(
            "Notes\n"
            "• Waterproof DS18B20 leads: red = VDD (3.3 V), yellow = DQ (data), black = GND.\n"
            "• One 4.7 kΩ pull-up (R1) for the whole bus, at TB1 where the four data wires meet.\n"
            "• The relay board plugs onto the header; the lines above are its header connections.\n"
            "• Wires that cross without a dot are not connected.",
            halign="left",
            valign="top",
            fontsize=NOTE,
        )
    )
    return d


def render_schematics(out_dir: Path) -> list[Path]:
    paths = []
    for name, fn in (("power", draw_power), ("control", draw_control)):
        path = out_dir / f"{name}.svg"
        fn().save(str(path))
        paths.append(path)
    return paths


def render_harness(out_dir: Path) -> None:
    # The wireviz console script, preferably the one next to this interpreter.
    wireviz = shutil.which("wireviz", path=str(Path(sys.executable).parent)) or shutil.which("wireviz")
    if not (wireviz and shutil.which("dot")):
        print("wireviz or Graphviz `dot` not found; skipping harness-*.svg", file=sys.stderr)
        return
    for src in sorted(HERE.glob("harness-*.yml")):
        subprocess.run([wireviz, "-f", "s", "-o", str(out_dir), str(src)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if power.svg / control.svg are out of date")
    args = ap.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            stale = [p.name for p in render_schematics(Path(tmp)) if p.read_bytes() != (HERE / p.name).read_bytes()]
        if stale:
            print(f"stale: {', '.join(stale)}; run python docs/wiring/build.py and commit", file=sys.stderr)
            return 1
        print("schematics up to date")
        return 0
    render_schematics(HERE)
    render_harness(HERE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
