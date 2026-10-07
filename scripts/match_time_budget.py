"""Where did an AIvAI match's time go? One report per seat plus the match.

Inputs (all optional except `--seat`):

- `--seat NAME=SOURCE` (repeat): that seat's wrapper `/metrics` text, SOURCE a
  URL or a file. Feeds the waits, model step seconds per role and promotions.
- `--agent-log NAME=FILE` (repeat): that seat's wrapper log (`docker compose
  logs agent-p1`). Feeds dispatch-to-first-acting-call (the `step tool=` lines
  run from turn start), baton refusals by code (`baton_refusal code=` lines,
  else the older `tool failed:` lines) and promotions (`baton_rotated` lines,
  else the metrics' `pass_baton` count).
- `--game-log FILE`: the game container's log, for the `match duration` line.

`--json` prints the report as JSON instead of text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

TARGET_SECONDS = 900.0
WAIT_TOOLS = ("pass_until", "wait_for_priority", "await_baton")
ROLES = ("orchestrator", "current", "future")
_BATON_CODE = re.compile(r"\b(BATON_[A-Z_]+)\b")
#: The MCP server's structured lines (`mcp.py`): one per refused acting call
#: and one per rotation (a promotion: the future takes over as current).
_REFUSAL_LINE = re.compile(r"\bbaton_refusal code=(?P<code>BATON_[A-Z_]+) tool=\S+ uid=\S+")
_ROTATED_LINE = re.compile(r"\bbaton_rotated uid=\S+ window_actions=\d+")
#: The baton-gated tools that change the game (`mcp.py`'s acting tools); a
#: wait, a read or a builtin is not an acting call.
ACTING_TOOLS = frozenset(
    {
        "cast", "suspend", "plot", "foretell", "cycle", "play_land", "activate", "equip",
        "level_up", "reconfigure", "turn_face_up", "bring_companion", "unlock_door",
        "declare_attackers", "declare_blockers", "decide", "pass", "pass_until", "run_script",
        "mulligan", "opening_hand_action", "declare_plan",
    }
)  # fmt: skip

_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)"
)
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_STEP = re.compile(
    r"\bstep tool=(?P<tool>\S+) model_ms=(?P<model>[\d.]+) tool_ms=(?P<tool_ms>[\d.]+)"
)
_DURATION = re.compile(
    r"match duration .*?seconds=(?P<s>[\d.]+) target_seconds=(?P<t>[\d.]+)"
    r" over_target=(?P<o>\w+)"
)


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    samples = []
    for line in text.splitlines():
        match = _SAMPLE.match(line.strip())
        if not match or line.lstrip().startswith("#"):
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        labels = dict(_LABEL.findall(match["labels"] or ""))
        samples.append((match["name"], labels, value))
    return samples


def _round(value: float) -> float:
    return round(value, 3)


def seat_from_metrics(text: str) -> dict:
    samples = parse_metrics(text)
    waits = {tool: {"calls": 0, "seconds": 0.0} for tool in WAIT_TOOLS}
    steps = dict.fromkeys(ROLES, 0.0)
    promotions = 0
    for name, labels, value in samples:
        if name in ("ccw_tool_seconds_count", "ccw_tool_seconds_sum"):
            tool = labels.get("tool", "")
            if tool in waits:
                if name.endswith("_count"):
                    waits[tool]["calls"] += int(value)
                else:
                    waits[tool]["seconds"] += value
            if tool == "pass_baton" and name.endswith("_count"):
                promotions += int(value)
        elif name == "ccw_model_step_seconds_sum":
            role = labels.get("role", "")
            steps[role] = steps.get(role, 0.0) + value
    for entry in waits.values():
        entry["seconds"] = _round(entry["seconds"])
    return {
        "waits": waits,
        "model_step_seconds": {role: _round(v) for role, v in steps.items()},
        "promotions": promotions,
    }


def seat_from_agent_log(text: str) -> dict:
    elapsed = 0.0
    first_acting: float | None = None
    for line in text.splitlines():
        step = _STEP.search(line)
        if not step or first_acting is not None:
            continue
        model = float(step["model"]) / 1000.0
        if step["tool"] in ACTING_TOOLS:
            first_acting = elapsed + model
        else:
            elapsed += model + float(step["tool_ms"]) / 1000.0
    # The structured lines are the first-class source; the wrapper's
    # `tool failed:` lines are the fallback for logs from before they existed
    # (counting both would count each refusal twice).
    structured = [m["code"] for m in _REFUSAL_LINE.finditer(text)]
    codes = structured or [
        m[1]
        for line in text.splitlines()
        if "tool failed:" in line
        for m in [_BATON_CODE.search(line)]
        if m
    ]
    refusals: dict[str, int] = {}
    for code in codes:
        refusals[code] = refusals.get(code, 0) + 1
    result: dict = {
        "dispatch_to_first_acting_seconds": None if first_acting is None else _round(first_acting),
        "baton_refusals": dict(sorted(refusals.items())),
    }
    rotations = len(_ROTATED_LINE.findall(text))
    if rotations:
        result["promotions"] = rotations
    return result


def match_from_log(text: str) -> dict:
    found = None
    for found in _DURATION.finditer(text):  # noqa: B007 - the last line wins
        pass
    if found is None:
        return {"seconds": None, "target_seconds": TARGET_SECONDS, "over_target": None}
    return {
        "seconds": float(found["s"]),
        "target_seconds": float(found["t"]),
        "over_target": found["o"] == "True",
    }


def build_report(seats: dict[str, str], agent_logs: dict[str, str], game_log: str) -> dict:
    report: dict = {"match": match_from_log(game_log), "seats": {}}
    for name, metrics_text in seats.items():
        seat = seat_from_metrics(metrics_text)
        seat.update(seat_from_agent_log(agent_logs.get(name, "")))
        if name not in agent_logs:
            seat["dispatch_to_first_acting_seconds"] = None
        report["seats"][name] = seat
    return report


def render_text(report: dict) -> str:
    match = report["match"]
    if match["seconds"] is None:
        lines = [f"match: no `match duration` line (target {match['target_seconds']:g}s)"]
    else:
        verdict = "OVER" if match["over_target"] else "within"
        lines = [
            f"match: {match['seconds']:.1f}s of {match['target_seconds']:g}s ({verdict} target)"
        ]
    for name, seat in report["seats"].items():
        first = seat["dispatch_to_first_acting_seconds"]
        lines.append(f"{name}:")
        lines.append(
            f"  dispatch->first acting call: {'n/a' if first is None else f'{first:.1f}s'}"
        )
        for tool, wait in seat["waits"].items():
            lines.append(f"  {tool}: {wait['seconds']:.1f}s over {wait['calls']} calls")
        steps = ", ".join(f"{role}={sec:.1f}s" for role, sec in seat["model_step_seconds"].items())
        lines.append(f"  model step seconds: {steps}")
        lines.append(f"  promotions: {seat['promotions']}")
        refusals = ", ".join(f"{c}={n}" for c, n in seat["baton_refusals"].items()) or "none"
        lines.append(f"  baton refusals: {refusals}")
    return "\n".join(lines)


def _read(source: str) -> str:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=10) as response:  # noqa: S310
            return response.read().decode()
    return Path(source).read_text()


def _pairs(values: list[str]) -> dict[str, str]:
    out = {}
    for value in values:
        name, sep, source = value.partition("=")
        if not sep or not name or not source:
            raise SystemExit(f"expected NAME=SOURCE, got {value!r}")
        out[name] = _read(source)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seat", action="append", default=[], metavar="NAME=URL_OR_FILE")
    parser.add_argument("--agent-log", action="append", default=[], metavar="NAME=FILE")
    parser.add_argument("--game-log", metavar="FILE")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.seat:
        parser.error("at least one --seat NAME=URL_OR_FILE is required")
    report = build_report(
        _pairs(args.seat),
        _pairs(args.agent_log),
        _read(args.game_log) if args.game_log else "",
    )
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
