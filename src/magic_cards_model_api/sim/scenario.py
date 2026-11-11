"""Scenario scripts for the sandbox: schema, expectations and state diff (pure code)."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from magic_cards_model_api.baseline_seat import decide_body
from magic_cards_model_api.sim.sandbox_game import ACTIONS, SandboxGame, SandboxSpec

ZONES = ("hand", "battlefield", "graveyard", "exile")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActArgs(_Strict):
    action: str
    player: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in ACTIONS:
            raise ValueError(f"unknown action {value!r}; known: {', '.join(ACTIONS)}")
        return value


class DecideArgs(_Strict):
    player: str
    answer: dict[str, Any]


class ExpectArgs(_Strict):
    life: dict[str, int] | None = None
    zones: dict[str, list[str]] | None = None
    step: str | None = None
    winner: str | None = None


class ActStep(_Strict):
    act: ActArgs


class DecideStep(_Strict):
    decide: DecideArgs


class ExpectStep(_Strict):
    expect: ExpectArgs


class NoteStep(_Strict):
    note: str


Step = ActStep | DecideStep | ExpectStep | NoteStep


class Scenario(_Strict):
    setup: SandboxSpec
    steps: list[Step]
    answers: Literal["stop", "baseline"] = "stop"


def validate_scenario(data: dict[str, Any], max_steps: int) -> Scenario:
    """Parse a scenario; raises ValueError (pydantic ValidationError is one) when invalid."""
    steps = data.get("steps") if isinstance(data, dict) else None
    if isinstance(steps, list) and len(steps) > max_steps:
        raise ValueError(
            f"scenario has {len(steps)} steps, more than the limit of {max_steps} "
            "(MTG_SIM_MAX_SCRIPT_STEPS)"
        )
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


# -- state helpers ---------------------------------------------------------


def _cards(state: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Card id -> (location "<player>.<zone>" or "stack", entry)."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, player in state.get("players", {}).items():
        for zone in ZONES:
            for card in player.get(zone, []):
                found[card["id"]] = (f"{name}.{zone}", card)
    for card in state.get("stack", []):
        found[card["id"]] = ("stack", card)
    return found


def _ref(card: dict[str, Any]) -> str:
    return f"@{card['label']}" if card.get("label") else card["name"]


def state_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What changed between two compact states; only non-empty keys."""
    diff: dict[str, Any] = {}
    life = {
        name: [before["players"][name]["life"], player["life"]]
        for name, player in after.get("players", {}).items()
        if name in before.get("players", {}) and before["players"][name]["life"] != player["life"]
    }
    old, new = _cards(before), _cards(after)
    moved, added, removed, tapped, untapped, counters = [], [], [], [], [], []
    for card_id, (where, card) in new.items():
        if card_id not in old:
            added.append({"card": _ref(card), "zone": where})
            continue
        was_where, was = old[card_id]
        if was_where != where:
            moved.append({"card": _ref(card), "from": was_where, "to": where})
        if bool(card.get("tapped")) != bool(was.get("tapped")):
            (tapped if card.get("tapped") else untapped).append(_ref(card))
        if (card.get("counters") or {}) != (was.get("counters") or {}):
            counters.append(
                {"card": _ref(card), "from": was.get("counters") or {},
                 "to": card.get("counters") or {}}
            )
    removed = [
        {"card": _ref(card), "zone": where}
        for card_id, (where, card) in old.items()
        if card_id not in new
    ]
    for key, value in (
        ("life", life), ("moved", moved), ("added", added), ("removed", removed),
        ("tapped", tapped), ("untapped", untapped), ("counters", counters),
    ):
        if value:
            diff[key] = value
    return diff


def _zone_cards(state: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
    """Cards of an expect zone key: "stack", or "<player>.<zone>" (zone after the last dot)."""
    if key == "stack":
        return state.get("stack", [])
    player, _, zone = key.rpartition(".")
    if zone not in ZONES or player not in state.get("players", {}):
        return None
    return state["players"][player].get(zone, [])


def _check_zone(
    key: str, wanted: list[str], state: dict[str, Any], labels: dict[str, str]
) -> str | None:
    cards = _zone_cards(state, key)
    if cards is None:
        return f"zones.{key}: unknown player or zone (zones: {', '.join(ZONES)}, or stack)"
    remaining = list(cards)
    missing: list[str] = []
    for item in sorted(wanted, key=lambda w: not w.startswith("@")):
        if item.startswith("@"):
            if item[1:] not in labels:
                return f"zones.{key}: unknown label {item}"
            match = next((c for c in remaining if c["id"] == labels[item[1:]]), None)
        else:
            match = next((c for c in remaining if c["name"] == item), None)
        if match is None:
            missing.append(item)
        else:
            remaining.remove(match)
    if not missing and not remaining:
        return None
    parts = []
    if missing:
        parts.append(f"missing {missing}")
    if remaining:
        parts.append(f"unexpected {[_ref(c) for c in remaining]}")
    return f"zones.{key}: {'; '.join(parts)}"


def check_expect(
    expect: dict[str, Any], state: dict[str, Any], labels: dict[str, str]
) -> list[str]:
    """Failure messages for every unmet expectation; empty when all hold."""
    fails: list[str] = []
    for name, want in (expect.get("life") or {}).items():
        player = state.get("players", {}).get(name)
        if player is None:
            fails.append(f"life.{name}: unknown player")
        elif player["life"] != want:
            fails.append(f"life.{name}: expected {want}, got {player['life']}")
    for key, wanted in (expect.get("zones") or {}).items():
        message = _check_zone(key, wanted, state, labels)
        if message:
            fails.append(message)
    for field in ("step", "winner"):
        if expect.get(field) is not None and state.get(field) != expect[field]:
            fails.append(f"{field}: expected {expect[field]!r}, got {state.get(field)!r}")
    return fails


def decide_as_act(step: DecideStep) -> ActStep:
    """`decide` is sugar for `act decide`."""
    return ActStep(act=ActArgs(action="decide", player=step.decide.player,
                               body=step.decide.answer))


# -- runner ----------------------------------------------------------------

_MAX_AUTO_DECISIONS = 50  # per gap between two steps; a decision loop must not eat the budget


def _refusal(result: Any) -> str | None:
    """The engine's reason when an action result is a refusal, else None."""
    if isinstance(result, dict) and False in (result.get("allowed"), result.get("accepted")):
        return str(result.get("reason") or "refused")
    return None


def _describe_decision(pending: dict[str, Any]) -> str:
    options = pending.get("options") or pending.get("cards") or []
    text = f"pending decision {pending.get('kind')} for {pending.get('player')}"
    if pending.get("prompt"):
        text += f" ({pending['prompt']})"
    if options:
        text += f"; options: {options}"
    return text


def run_scenario(game: SandboxGame, scenario: Scenario, deadline: float) -> dict[str, Any]:
    """Create the sandbox from `scenario.setup` and run its steps against the real engine.

    `deadline` is a `time.monotonic()` instant. Stops at the first refused act, at a pending
    decision the next act does not answer (`answers == "stop"`), or at the deadline; failed
    expectations are recorded and never stop the run.
    """
    budget = max(0.0, deadline - time.monotonic())
    created = game.create(scenario.setup.model_dump())
    initial = created["state"]
    state, labels = initial, created["labels"]
    records: list[dict[str, Any]] = []
    stopped_at: int | None = None
    stop_reason: str | None = None

    def stop(index: int, reason: str) -> None:
        nonlocal stopped_at, stop_reason
        stopped_at, stop_reason = index, reason

    for index, step in enumerate(scenario.steps):
        if time.monotonic() >= deadline:
            stop(index, f"time budget of {budget:g} s used up")
            break
        if isinstance(step, NoteStep):
            records.append({"index": index, "kind": "note", "ok": True, "result": step.note})
            continue
        if isinstance(step, ExpectStep):
            fails = check_expect(step.expect.model_dump(exclude_none=True), state, labels)
            record: dict[str, Any] = {"index": index, "kind": "expect", "ok": not fails}
            if fails:
                record["expect_failures"] = fails
            records.append(record)
            continue
        act = decide_as_act(step) if isinstance(step, DecideStep) else step
        if act.act.action != "decide":
            pending = state.get("pending_decision")
            auto = 0
            while pending and scenario.answers == "baseline" and auto < _MAX_AUTO_DECISIONS:
                body = decide_body(pending, pending["player"])
                answer = game.act("decide", pending["player"], body)
                state, labels = answer["state"], answer["labels"]
                reason = _refusal(answer["result"])
                records.append(
                    {"index": index, "kind": "auto_decide", "ok": reason is None,
                     "result": answer["result"], **({"error": reason} if reason else {})}
                )
                if reason:
                    break
                auto += 1
                pending = state.get("pending_decision")
            if pending:
                stop(index, _describe_decision(pending) + "; answer it with a decide step first")
                break
        args = act.act
        answer = game.act(args.action, args.player, args.body, args.dry_run)
        state, labels = answer["state"], answer["labels"]
        reason = _refusal(answer["result"])
        record = {"index": index, "kind": "decide" if isinstance(step, DecideStep) else "act",
                  "ok": reason is None, "result": answer["result"]}
        if reason:
            record["error"] = reason
        records.append(record)
        if reason:
            stop(index, f"step {index} ({args.action}) was refused: {reason}")
            break

    final = game.state()
    return {
        "steps": records,
        "stopped_at": stopped_at,
        "stop_reason": stop_reason,
        "initial_state": initial,
        "final_state": final["state"],
        "diff": state_diff(initial, final["state"]),
        "labels": final["labels"],
        **{k: v for k, v in final.items() if k in ("support_warnings", "support_note")},
    }
