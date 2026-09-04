# Play UI and Action Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `ui/app.js` with an MTGO-like play screen: one click when nothing is left to choose, per-slot targeting that lets Kozilek's Command take the same player twice, auto-paid mana with a preview and a click-to-pin override, tiered priority passing with persisted stops, inline combat, and a game log; add the additive API fields that make it possible.

**Architecture:** The server stays the only authority on rules; the browser reads per-card hints from the state view and on-demand affordances, and renders typed prompts. The UI is buildless ES modules under `ui/`: pure modules (`actions.js`, `targeting.js`, `mana.js`, `priority.js`, `decisions.js`) that load under Node for tests, and DOM modules (`dom.js`, `render/*.js`) that patch keyed children instead of rewriting `innerHTML`. Polling every 1.2 s stays.

**Tech Stack:** Python 3.13, FastAPI, pydantic, pytest; plain ES2022 JavaScript modules, no npm; Node on PATH for the UI unit tests (already required by `tests/test_ui_*.py`).

**Spec:** `docs/superpowers/specs/2026-09-04-play-ui-action-protocol-design.md`. Repo: `/Users/szymonri/Documents/magic-cards-model-api`, branch `play-ui-and-agent-pods`. Engine is the sibling `../magic-cards-model` (do not modify it in this plan).

## Global Constraints

- No mocks, stubs, or hand-written card data in Python tests: every board is a real game booted through `TestClient` with real Scryfall fixtures via `conftest.add_card` (see `tests/test_modal_x_cast.py` for the pattern). Node tests feed JSON shaped exactly like the API's responses, captured from real calls where the test needs a board.
- Every CR number written into code or a docstring must be verified with `knowledge_search(q=..., type="rules")` in the session that writes it (project rule).
- Buildless: no `package.json`, no bundler, no framework. `ui/index.html` loads `app.js` as `type="module"`.
- Additive API only: `seatkit.py`, `mcp.py`, `seat_cli.py` are not modified; `PROTOCOL_VERSION` does not change.
- Writing: plain hyphens and straight quotes, no em dashes, in every file touched.
- Commands: `mise run test`, `mise run lint`, `mise run fmt` from the api repo. Run `uv run ruff format .` before every commit that touches Python.
- Commit after every task with the message given; no attribution lines.

## File map

Python (modify):
- `src/magic_cards_model_api/affordances.py` - `_profile_view` gains `mode`/`mode_text` per requirement via the engine's `profile_for_modes_with_owners`.
- `src/magic_cards_model_api/engine_api.py` - dry-run detail gains `tapped`; `play_land` gains `dry_run`; `_drive_cast` applies `shares`.
- `src/magic_cards_model_api/schemas.py` - `CastRequest.shares`, `PlayLandRequest.dry_run`.
- `src/magic_cards_model_api/serialize.py` - stack objects and cards gain `target_refs`.
- `src/magic_cards_model_api/decisions.py` - `PendingDecision.target_minimum`.
- `src/magic_cards_model_api/app.py` - `GET /log?since=`, `play-land` dry-run audit.

JavaScript (create under `ui/`): `api.js`, `store.js`, `dom.js`, `symbols.js`, `cards.js`, `actions.js`, `targeting.js`, `mana.js`, `priority.js`, `decisions.js`, `combat.js`, `render/board.js`, `render/ribbon.js`, `render/log.js`, `render/lobby.js`, `app.js` (replaces the old file), `index.html`, `style.css`.

Tests (create): `tests/ui_node.py` (helper), `tests/test_affordance_modes.py`, `tests/test_dry_run_tapped.py`, `tests/test_play_land_dry_run.py`, `tests/test_cast_shares.py`, `tests/test_stack_target_refs.py`, `tests/test_decision_target_minimum.py`, `tests/test_log_since.py`, `tests/test_ui_targeting.py`, `tests/test_ui_mana.py`, `tests/test_ui_actions.py`, `tests/test_ui_priority.py`, `tests/test_ui_decisions.py`, `tests/test_ui_combat.py`, `tests/test_kozilek_command_ui_flow.py`.

Tests (rewrite against the new modules, same intent): `tests/test_ui_autopilot.py`, `tests/test_ui_autopilot_attackers.py`, `tests/test_ui_autopilot_blockers.py`, `tests/test_ui_autopilot_next_occurrence.py`, `tests/test_ui_priority_panel.py`, `tests/test_ui_targets.py`, `tests/test_ui_ai_stall.py`, `tests/test_ui_pregame_panel.py`, `tests/test_ui_decision_coverage.py`, `tests/test_ui_hidden_elements.py`, `tests/test_ui_routes.py`, and the renderer halves of `tests/test_card_split_labels.py`, `tests/test_fetchland_search_choice.py`, `tests/test_trigger_order_labels.py`.

---

## Part 1: protocol additions (Python)

### Task 1: `mode` and `mode_text` on affordance requirements

**Files:**
- Modify: `src/magic_cards_model_api/affordances.py:65-92` (`_profile_view`), `:228-267` (`_profile_for`)
- Test: `tests/test_affordance_modes.py`

**Interfaces:**
- Consumes: engine `magic_cards_model.machines.targeting.profile_for_modes_with_owners(card, chosen, x_value) -> (TargetingProfile, list[int])` and `modes.modal_profile(card).resolve(modes)`.
- Produces: every entry of `requirements` in `GET /api/games/{id}/affordances/{card_id}` carries `mode: int | None` (1-based mode number) and `mode_text: str | None`.

- [ ] **Step 1: Write the failing test**

```python
"""Each targeting requirement says which mode it came from (CR 700.2f).

Kozilek's Command, modes 1 and 2, has two "target player" requirements that
look identical on the wire; a client filling slots needs to know one belongs
to mode 1 and the other to mode 2. Board and card fixtures follow
`tests/test_modal_x_cast.py`.
"""

from __future__ import annotations

from conftest import add_card, find
from fastapi.testclient import TestClient


def boot(client: TestClient) -> tuple[str, str]:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    command = add_card(client, game_id, "alice", "Kozilek's Command", zone="hand")
    add_card(client, game_id, "alice", "Memnite", zone="battlefield")
    for _ in range(4):
        add_card(client, game_id, "alice", "Wastes", zone="battlefield")
    return game_id, command


def test_requirements_carry_their_mode(client: TestClient) -> None:
    game_id, command = boot(client)
    result = client.get(
        f"/api/games/{game_id}/affordances/{command}",
        params={"player": "alice", "modes": "1,2", "x_value": 1},
    ).json()
    assert [r["mode"] for r in result["requirements"]] == [1, 2]
    assert all(r["mode_text"].startswith("Target player") for r in result["requirements"])
    by_id = {c["id"]: c["requirements"] for c in result["candidates"]}
    assert by_id["alice"] == [0, 1]
    assert by_id["bob"] == [0, 1]


def test_non_modal_requirements_have_no_mode(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    bolt = add_card(client, game_id, "alice", "Lightning Bolt", zone="hand")
    result = client.get(
        f"/api/games/{game_id}/affordances/{bolt}", params={"player": "alice"}
    ).json()
    assert [r["mode"] for r in result["requirements"]] == [None]
    assert result["requirements"][0]["mode_text"] is None
```

If `Kozilek's Command`, `Memnite`, `Wastes` or `Lightning Bolt` is not in `tests/fixtures/scryfall`, check `conftest.add_card`'s fixture directory and run `uv run python scripts/add_card.py "<name>"` in this repo if such a script exists; otherwise copy the fixture the way `tests/test_modal_x_cast.py` obtained its cards. `find` is imported for parity with sibling tests and may be removed if unused.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_affordance_modes.py -v`
Expected: FAIL with `KeyError: 'mode'`.

- [ ] **Step 3: Implement**

In `affordances.py`, change `_profile_for` to return owners alongside the profile, and `_profile_view` to accept them:

```python
from magic_cards_model.machines.targeting import (
    TargetingProfile,
    profile_for_modes_with_owners,
    profile_for_text,
    targeting_profile,
)


def _profile_view(
    profile: TargetingProfile, mode_of: list[tuple[int | None, str | None]] | None = None
) -> dict[str, Any]:
    """A targeting profile as the UI needs it.

    `mode_of` has one `(mode_number, mode_text)` per requirement, from
    `profile_for_modes_with_owners` (CR 700.2f: each mode may target on its
    own); a non-modal profile reports `None` for both.
    """
    mode_of = mode_of or [(None, None)] * len(profile.requirements)
    return {
        "targets_anything": profile.targets_anything,
        "understood": profile.understood,
        "minimum": profile.min_targets,
        "maximum": profile.max_targets,
        "requirements": [
            {
                "phrase": requirement.phrase,
                "count": requirement.count,
                "minimum": requirement.minimum,
                "unbounded": requirement.unbounded,
                "variable_count": requirement.variable_count,
                "mode": mode,
                "mode_text": mode_text,
            }
            for requirement, (mode, mode_text) in zip(
                profile.requirements, mode_of, strict=True
            )
        ],
        "division": (
            {"amount": profile.division.amount, "what": profile.division.what}
            if profile.division is not None
            else None
        ),
    }
```

And in `_profile_for`, replace the modal branch:

```python
    modal = modal_profile(state.card)
    if modes and modal is not None:
        chosen = modal.resolve(modes)
        profile, owners = profile_for_modes_with_owners(state.card, chosen, x_value)
        mode_of = [(chosen[owner].number, chosen[owner].text) for owner in owners]
        return profile, mode_of
```

Change `_profile_for`'s signature to `-> tuple[TargetingProfile, list[tuple[int | None, str | None]] | None]`, returning `(profile, None)` on the other branches, and update the single caller in `candidates`:

```python
        profile, mode_of = _profile_for(game, state, caster, ability_index, modes, x_value)
    ...
    view = _profile_view(profile, mode_of)
```

`modal.resolve` returns `Mode` objects in the order given (see `machines/modes.py`); if it returns a tuple, `list(...)` it first.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_affordance_modes.py tests/test_affordances.py tests/test_modal_x_cast.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Say which mode each targeting requirement belongs to"
```

### Task 2: `tapped` in dry-run detail for cast and activate

**Files:**
- Modify: `src/magic_cards_model_api/engine_api.py:320-332` (`_drive_cast` dry run), `:666-676` (`activate_ability` dry run)
- Test: `tests/test_dry_run_tapped.py`

**Interfaces:**
- Consumes: engine `payment.tapped_for_mana: list[CardState]` (filled by `auto_tap` and `tap_for_mana`, emptied by `rewind`).
- Produces: dry-run responses of `POST .../actions/cast` and `.../activate` carry `tapped: [{"card_id": str, "name": str}]`, read before `rewind()`.

- [ ] **Step 1: Write the failing test**

```python
"""A dry run says which sources auto-pay would tap, so the UI can preview
them before the cast fires (CR 601.2g: mana abilities are activated while
paying)."""

from __future__ import annotations

from conftest import add_card
from fastapi.testclient import TestClient


def test_dry_run_cast_reports_the_lands_it_would_tap(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    bolt = add_card(client, game_id, "alice", "Lightning Bolt", zone="hand")
    mountain = add_card(client, game_id, "alice", "Mountain", zone="battlefield")
    island = add_card(client, game_id, "alice", "Island", zone="battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    res = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain, island],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is True, res
    assert res["tapped"] == [{"card_id": mountain, "name": "Mountain"}]
    board = client.get(f"/api/games/{game_id}").json()
    assert all(not c["tapped"] for c in board["battlefield"])


def test_dry_run_activate_reports_tapped_sources(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    mountain = add_card(client, game_id, "alice", "Mountain", zone="battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    res = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={"player": "alice", "card_id": mountain, "ability_index": 0, "dry_run": True},
    ).json()
    assert res["allowed"] is True, res
    assert res["tapped"] == []
```

Check `tests/conftest.py` for the exact `PUT .../step` body shape and adjust the two `client.put` lines to match it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dry_run_tapped.py -v`
Expected: FAIL with `KeyError: 'tapped'`.

- [ ] **Step 3: Implement**

Add a helper near `shortfall_reason` in `engine_api.py`:

```python
def tapped_view(payment: Any) -> list[dict[str, str]]:
    """Which sources this payment has tapped so far, oldest first - read
    before `rewind()` on a dry run, so a client can preview auto-pay."""
    return [
        {"card_id": str(source.id), "name": source.name}
        for source in getattr(payment, "tapped_for_mana", [])
    ]
```

Then in both dry-run branches (`_drive_cast` and `activate_ability`), replace the two `Outcome(...)` lines with:

```python
        if req.dry_run:
            tapped = tapped_view(payment)
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost), "tapped": tapped})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost), "tapped": tapped})
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_dry_run_tapped.py tests/test_auto_mana.py tests/test_auto_tap_colours.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Report which sources a dry run would tap"
```

### Task 3: `dry_run` on play-land

**Files:**
- Modify: `src/magic_cards_model_api/schemas.py:236-239` (`PlayLandRequest`), `src/magic_cards_model_api/engine_api.py:745-762` (`play_land`), `src/magic_cards_model_api/app.py:877-884` (`action_play_land`)
- Test: `tests/test_play_land_dry_run.py`

**Interfaces:**
- Consumes: engine `Game.why_cannot_play_land(land, player) -> str | None` if it exists; otherwise `game.play_land` raises `ValueError`. Check with `grep -n "def why_cannot_play_land\|def can_play_land" ../magic-cards-model/src/magic_cards_model/models/game.py`. If neither exists, implement the dry run by snapshotting: `saved = land.zone; try play; then move back` is NOT acceptable (it fires triggers). Instead, use `game.land_plays_remaining(player)`-style checks if present, and if none exist, fall back to the refusal-only probe below.
- Produces: `POST .../actions/play-land` with `dry_run: true` returns `{allowed, reason?}` without changing the board.

- [ ] **Step 1: Write the failing test**

```python
"""`play-land` can be asked without being done, like every other action."""

from __future__ import annotations

from conftest import add_card
from fastapi.testclient import TestClient


def boot(client: TestClient) -> tuple[str, str]:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    land = add_card(client, game_id, "alice", "Mountain", zone="hand")
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    return game_id, land


def test_dry_run_allows_without_playing(client: TestClient) -> None:
    game_id, land = boot(client)
    res = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card_id": land, "dry_run": True},
    ).json()
    assert res["allowed"] is True, res
    board = client.get(f"/api/games/{game_id}").json()
    assert board["battlefield"] == []
    assert len(board["hands"]["alice"]) == 1


def test_dry_run_refuses_a_second_land_with_the_engines_reason(client: TestClient) -> None:
    game_id, land = boot(client)
    second = add_card(client, game_id, "alice", "Mountain", zone="hand")
    assert client.post(
        f"/api/games/{game_id}/actions/play-land", json={"player": "alice", "card_id": land}
    ).json()["allowed"]
    res = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card_id": second, "dry_run": True},
    ).json()
    assert res["allowed"] is False
    assert res["reason"]
    board = client.get(f"/api/games/{game_id}").json()
    assert len(board["battlefield"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_play_land_dry_run.py -v`
Expected: FAIL (422 for the unknown `dry_run` field, or the land is played).

- [ ] **Step 3: Implement**

`schemas.py`:

```python
class PlayLandRequest(BaseModel):
    player: str
    card_id: UUID | None = None
    card: str | None = None
    dry_run: bool = False
```

`engine_api.play_land`: find the engine's permission probe. Run `grep -n "def why_cannot_play_land\|def can_play_land\|land_plays" ../magic-cards-model/src/magic_cards_model/models/game.py`. Use whichever probe exists:

```python
def play_land(game: Game, req: PlayLandRequest) -> Outcome:
    try:
        land = locate_land(game, req)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    if req.dry_run:
        refusal = game.why_cannot_play_land(land, req.player)  # adapt to the probe found
        return Outcome(refusal is None, refusal)
    try:
        played = game.play_land(land, req.player)
    except ValueError as exc:
        return refused(exc)
    return Outcome(True, detail={"card_id": str(played.id)})
```

If no probe exists in the engine, `game.play_land` itself must be read to see which checks it runs before mutating (CR 305.1, 305.2, 116.2a, priority, main phase, empty stack); reproduce exactly those checks in a `_land_play_refusal(game, land, player) -> str | None` helper in `engine_api.py`, each returning the same wording `game.play_land` raises, and note in its docstring that it mirrors `Game.play_land` and must be kept in step.

`app.py` `action_play_land`: `audit_outcome` already skips logging when `outcome.dry_run`? Check `audit_outcome` (app.py:265). If it keys off a `dry_run` attribute on `Outcome`, set it: `Outcome(..., dry_run=True)` the way `cast_card` does; otherwise pass `dry_run=req.dry_run` the way `action_cast` does.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_play_land_dry_run.py tests/test_action_flow.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Let play-land be asked as a dry run"
```

### Task 4: `shares` on a cast (CR 601.2d division)

**Files:**
- Modify: `src/magic_cards_model_api/schemas.py:93-135` (`CastRequest`), `src/magic_cards_model_api/engine_api.py:305-311` (`_drive_cast` after targets)
- Test: `tests/test_cast_shares.py`

**Interfaces:**
- Consumes: engine `Casting.announce_division(shares: Iterable[int])`, one entry per announced target in order; must run after `choose_targets` and after `determine_cost` when X is involved (its docstring says `x_value` must already be set for a variable division). Order used here: modes, targets, determine_cost, then `announce_division`.
- Produces: `CastRequest.shares: list[int] | None`.

- [ ] **Step 1: Write the failing test**

```python
"""Electrolyze's 2 damage split 1 and 1 (CR 601.2d), announced with the cast."""

from __future__ import annotations

from conftest import add_card
from fastapi.testclient import TestClient


def test_shares_are_announced_and_dealt(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    electrolyze = add_card(client, game_id, "alice", "Electrolyze", zone="hand")
    memnite = add_card(client, game_id, "bob", "Memnite", zone="battlefield")
    for name in ("Island", "Mountain", "Mountain"):
        add_card(client, game_id, "alice", name, zone="battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    res = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": electrolyze,
            "targets": [memnite, "bob"],
            "shares": [1, 1],
            "mana_sources": [c["id"] for c in client.get(f"/api/games/{game_id}").json()["battlefield"] if c["controller"] == "alice"],
        },
    ).json()
    assert res["allowed"] is True, res
    client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "alice"})
    client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "bob"})
    board = client.get(f"/api/games/{game_id}").json()
    assert all(c["id"] != memnite for c in board["battlefield"])
    bob = next(p for p in board["players"] if p["username"] == "bob")
    assert bob["life"] == 19


def test_shares_that_do_not_add_up_are_refused(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    electrolyze = add_card(client, game_id, "alice", "Electrolyze", zone="hand")
    memnite = add_card(client, game_id, "bob", "Memnite", zone="battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    res = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card_id": electrolyze, "targets": [memnite, "bob"], "shares": [2, 1], "dry_run": True},
    ).json()
    assert res["allowed"] is False
    assert "divide" in res["reason"].lower() or "601.2d" in res["reason"]
```

Confirm the engine's Electrolyze effect is registered in `machines/effects.py` (`grep -n "Electrolyze" ../magic-cards-model/src/magic_cards_model/machines/effects.py`); if it is not, use a card that is (`grep -n "division" ../magic-cards-model/tests/scenarios/*.py` lists the divided-damage scenarios) and adjust the expected life and creature.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cast_shares.py -v`
Expected: FAIL with 422 (unknown field `shares`).

- [ ] **Step 3: Implement**

`schemas.py`, in `CastRequest` after `x_value`:

```python
    shares: list[int] | None = None
    """CR 601.2d: how a divided effect (Electrolyze's damage, distributed
    counters) is split among `targets`, one entry per target in the same
    order. Only for a card whose affordance carries `division`."""
```

`engine_api._drive_cast`, after `determine_cost` and before `apply_object_costs`:

```python
        if req.shares is not None:
            casting.announce_division(req.shares)
```

`announce_division` raises `ValueError` for a bad split; that is inside the `try` that already returns `refused(exc)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cast_shares.py tests/test_cast.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Announce a divided effect's shares with the cast"
```

### Task 5: `target_refs` on stack objects and cards

**Files:**
- Modify: `src/magic_cards_model_api/serialize.py:144-160` (`_target_names`), `:325-345` (`_card_view`), `:378-390` (stack ability view), `:455-470` (snapshot views)
- Test: `tests/test_stack_target_refs.py`

**Interfaces:**
- Produces: next to the existing `targets: [names]`, every card view and stack ability view carries `target_refs: [{"kind": "card"|"player"|"ability", "id": str, "name": str}]`.

- [ ] **Step 1: Write the failing test**

```python
"""A client can tell what a spell on the stack is aimed at by id, not only by name."""

from __future__ import annotations

from conftest import add_card
from fastapi.testclient import TestClient


def test_bolt_on_the_stack_names_bob_by_id(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    bolt = add_card(client, game_id, "alice", "Lightning Bolt", zone="hand")
    mountain = add_card(client, game_id, "alice", "Mountain", zone="battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    res = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card_id": bolt, "targets": ["bob"], "mana_sources": [mountain]},
    ).json()
    assert res["allowed"], res
    (spell,) = client.get(f"/api/games/{game_id}").json()["stack"]
    assert spell["targets"] == ["bob"]
    assert spell["target_refs"] == [{"kind": "player", "id": "bob", "name": "bob"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stack_target_refs.py -v`
Expected: FAIL with `KeyError: 'target_refs'`.

- [ ] **Step 3: Implement**

In `serialize.py`, add beside `_target_names`:

```python
def _target_refs(game: Game, targets: list[Any]) -> list[dict[str, str]]:
    """`_target_names` with ids: what a client needs to highlight the target
    on the board rather than only print its name."""
    from magic_cards_model.models import Player as PlayerModel

    refs = []
    for ref in targets:
        try:
            thing = game.resolve_target(ref)
        except Exception:
            thing = None
        if isinstance(thing, CardState):
            refs.append({"kind": "card", "id": str(thing.id), "name": thing.name})
        elif isinstance(thing, PlayerModel):
            refs.append({"kind": "player", "id": thing.username, "name": thing.username})
        elif isinstance(thing, AbilityOnStack):
            refs.append(
                {"kind": "ability", "id": str(thing.id), "name": f"ability from {thing.source_name}"}
            )
        else:
            refs.append({"kind": "unknown", "id": str(ref), "name": str(ref)})
    return refs
```

Then everywhere `"targets": _target_names(game, X)` appears in `_card_view`, the stack ability view and the two snapshot views, add the line `"target_refs": _target_refs(game, X),` directly after it.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_stack_target_refs.py tests/test_log_snapshots.py tests/test_games.py -v`
Expected: PASS. If a snapshot test compares exact dicts, update its expected data to include `target_refs`.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Carry target ids on stack objects next to their names"
```

### Task 6: `target_minimum` on pending decisions

**Files:**
- Modify: `src/magic_cards_model_api/decisions.py:443-460` (`PendingDecision.to_view`) and the `PendingDecision` construction site that already reads `target_maximum` off the engine `Decision`
- Test: `tests/test_decision_target_minimum.py`

**Interfaces:**
- Produces: `pending_decision.target_minimum: int | None` for `choose_targets`.

- [ ] **Step 1: Write the failing test**

Find an existing `choose_targets` decision test to copy the board from: `grep -ln "choose_targets" tests/*.py`. `tests/test_activate_target_decisions.py` boots one. Copy its boot helper into this file (do not import a test module's fixtures unless it already exports them), then:

```python
def test_choose_targets_decision_says_its_minimum(client: TestClient) -> None:
    game_id, uid = boot_a_choose_targets_decision(client)  # from the copied helper
    pending = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()["pending_decision"]
    assert pending["kind"] == "choose_targets"
    assert pending["target_minimum"] == 1
    assert pending["target_maximum"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_decision_target_minimum.py -v`
Expected: FAIL with `KeyError: 'target_minimum'`.

- [ ] **Step 3: Implement**

Add `target_minimum: int | None = None` to the `PendingDecision` dataclass/model next to `target_maximum`, set it where `target_maximum` is set from the engine `Decision` (`grep -n "target_maximum=" decisions.py`), and add `"target_minimum": self.target_minimum,` to `to_view`. Delete the sentence in the `current()` docstring (decisions.py:655-666) that says `PendingDecision` does not carry it.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_decision_target_minimum.py tests/test_decisions.py tests/test_decision_validator_drift.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Tell a seat how many targets a decision needs at least"
```

### Task 7: `GET /log?since=`

**Files:**
- Modify: `src/magic_cards_model_api/app.py:426-444` (`game_log`)
- Test: `tests/test_log_since.py`

**Interfaces:**
- Produces: `GET /api/games/{id}/log?uid=&since=<seq>` returns only entries with `seq > since`, plus `"last_seq"`.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from fastapi.testclient import TestClient


def test_since_returns_only_newer_entries(client: TestClient) -> None:
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game_id = created["game_id"]
    full = client.get(f"/api/games/{game_id}/log").json()
    last = full["last_seq"]
    assert last == full["log"][-1]["seq"]
    client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "alice"})
    newer = client.get(f"/api/games/{game_id}/log", params={"since": last}).json()
    assert newer["log"], newer
    assert all(entry["seq"] > last for entry in newer["log"])
    assert newer["last_seq"] >= last + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_log_since.py -v`
Expected: FAIL with `KeyError: 'last_seq'`.

- [ ] **Step 3: Implement**

```python
@app.get("/api/games/{game_id}/log")
def game_log(game_id: str, uid: str | None = None, since: int = 0) -> dict[str, Any]:
    """The action log. ... (keep the existing docstring) ...

    `since` is the last `seq` the caller already has; only newer entries
    come back, and `last_seq` says where to resume next time.
    """
    game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    log = audit.entries(game_id)
    last_seq = log[-1]["seq"] if log else 0
    log = [entry for entry in log if entry["seq"] > since]
    if isinstance(viewer, str):
        log = [visibility.redact_log_entry(entry, viewer) for entry in log]
    return {"log": log, "last_seq": last_seq}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_log_since.py tests/test_audit_log.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Let a log reader ask only for what is new"
```

---

## Part 2: pure UI modules (JavaScript, Node-tested)

### Task 8: Node test helper and `targeting.js`

**Files:**
- Create: `tests/ui_node.py`, `ui/targeting.js`
- Test: `tests/test_ui_targeting.py`

**Interfaces:**
- Produces (`tests/ui_node.py`): `run_module(module: str, expression: str, **bindings) -> Any` - imports `ui/<module>` under Node as an ES module bound to `m`, binds each keyword as a JSON const, evaluates `expression`, returns the JSON result.
- Produces (`ui/targeting.js`):
  - `slotsFromAffordance(affordance) -> Slot[]` where `Slot = {index, requirementIndex, label, count: number|null, minimum: number, mode: number|null, targets: string[]}`; one slot per requirement.
  - `activeSlotIndex(slots) -> number` (first slot below its minimum, else first bounded slot with room, else first unbounded slot, else -1).
  - `canFill(affordance, slot, id) -> boolean` (the candidate lists `slot.requirementIndex`, and the slot has room).
  - `fill(slots, slotIndex, id) -> Slot[]` (new array; no-op when it cannot fill).
  - `unfill(slots, slotIndex, id) -> Slot[]`.
  - `flatten(slots) -> string[]` (slot order, then insertion order).
  - `isSatisfied(slots) -> boolean` (every slot at or above `minimum`).
  - `candidateIds(affordance, slot) -> Set<string>`.
  - `sharesValid(shares: number[], amount: number) -> boolean` (every share >= 1, sum == amount).

- [ ] **Step 1: Write the helper**

```python
"""Run one of `ui/*.js`'s ES modules under Node and evaluate an expression.

The browser is served these files as-is (no build step), so the tests run the
exact modules it runs. `bindings` become JSON consts in the script's scope,
and the module is bound to `m`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[1] / "ui"

requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def run_module(module: str, expression: str, **bindings: Any) -> Any:
    consts = "\n".join(f"const {name} = {json.dumps(value)};" for name, value in bindings.items())
    script = f"""
    import {{ pathToFileURL }} from "node:url";
    const m = await import(pathToFileURL({json.dumps(str(UI / module))}).href);
    {consts}
    const __result = await ({expression});
    process.stdout.write(JSON.stringify(__result === undefined ? null : __result));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout or "null")
```

- [ ] **Step 2: Write the failing tests**

```python
"""Per-slot targeting: Kozilek's Command's two "target player" modes can both
take the same player (CR 115.3 forbids a repeat only within one instance of
the word "target"). The affordance JSON below is the shape
`GET /affordances` returns for modes 1,2 at X=1 (`tests/test_affordance_modes.py`)."""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

KOZILEK_12 = {
    "targets_anything": True,
    "understood": True,
    "minimum": 2,
    "maximum": 2,
    "requirements": [
        {"phrase": "target player", "count": 1, "minimum": 1, "unbounded": False,
         "variable_count": False, "mode": 1, "mode_text": "Target player creates X 0/1 colorless Eldrazi Spawn creature tokens with \"Sacrifice this token: Add {C}.\""},
        {"phrase": "target player", "count": 1, "minimum": 1, "unbounded": False,
         "variable_count": False, "mode": 2, "mode_text": "Target player scries X, then draws a card."},
    ],
    "division": None,
    "candidates": [
        {"id": "alice", "kind": "player", "name": "alice", "zone": None, "controller": None, "requirements": [0, 1]},
        {"id": "bob", "kind": "player", "name": "bob", "zone": None, "controller": None, "requirements": [0, 1]},
    ],
}

UP_TO_X = {
    "targets_anything": True, "understood": True, "minimum": 0, "maximum": 2,
    "requirements": [{"phrase": "up to 2 target cards from graveyards", "count": 2, "minimum": 0,
                      "unbounded": False, "variable_count": False, "mode": 4, "mode_text": "Exile up to X target cards from graveyards."}],
    "division": None,
    "candidates": [{"id": "g1", "kind": "card", "name": "Memnite", "zone": "graveyard", "controller": "bob", "requirements": [0]},
                   {"id": "g2", "kind": "card", "name": "Wastes", "zone": "graveyard", "controller": "bob", "requirements": [0]}],
}


def test_same_player_fills_both_mode_slots() -> None:
    flat = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); s = m.fill(s, 0, 'alice'); s = m.fill(s, 1, 'alice'); return [m.flatten(s), m.isSatisfied(s)]; })()",
        aff=KOZILEK_12,
    )
    assert flat == [["alice", "alice"], True]


def test_slot_labels_name_their_mode() -> None:
    labels = run_module("targeting.js", "m.slotsFromAffordance(aff).map(s => s.label)", aff=KOZILEK_12)
    assert labels == ["Mode 1: target player", "Mode 2: target player"]


def test_active_slot_advances_as_slots_fill() -> None:
    steps = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); const a = m.activeSlotIndex(s); s = m.fill(s, 0, 'bob'); const b = m.activeSlotIndex(s); s = m.fill(s, 1, 'bob'); return [a, b, m.activeSlotIndex(s)]; })()",
        aff=KOZILEK_12,
    )
    assert steps == [0, 1, -1]


def test_a_full_slot_refuses_a_second_target() -> None:
    result = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); s = m.fill(s, 0, 'bob'); return [m.canFill(aff, s[0], 'alice'), m.flatten(m.fill(s, 0, 'alice'))]; })()",
        aff=KOZILEK_12,
    )
    assert result == [False, ["bob"]]


def test_unfill_removes_one_target() -> None:
    result = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); s = m.fill(s, 0, 'g1'); s = m.fill(s, 0, 'g2'); s = m.unfill(s, 0, 'g1'); return m.flatten(s); })()",
        aff=UP_TO_X,
    )
    assert result == ["g2"]


def test_up_to_slots_are_satisfied_empty() -> None:
    assert run_module("targeting.js", "m.isSatisfied(m.slotsFromAffordance(aff))", aff=UP_TO_X) is True


def test_a_non_candidate_cannot_fill() -> None:
    assert run_module("targeting.js", "m.canFill(aff, m.slotsFromAffordance(aff)[0], 'nobody')", aff=KOZILEK_12) is False


def test_candidate_ids_follow_the_slot() -> None:
    ids = run_module("targeting.js", "[...m.candidateIds(aff, m.slotsFromAffordance(aff)[1])].sort()", aff=KOZILEK_12)
    assert ids == ["alice", "bob"]


def test_shares_must_sum_to_the_amount_and_be_positive() -> None:
    assert run_module("targeting.js", "[m.sharesValid([1, 1], 2), m.sharesValid([2, 0], 2), m.sharesValid([1, 2], 2)]") == [True, False, False]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_ui_targeting.py -v`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement `ui/targeting.js`**

```js
// Per-slot targeting (CR 601.2c, CR 700.2f). One slot per requirement the
// affordance lists; a modal spell's slots are labelled by the mode they came
// from. The same object may sit in two different slots: CR 115.3 forbids a
// repeat only within one instance of the word "target", and the server's
// affordance already lists such an object under both requirements.
//
// Pure: no DOM, no fetch. `fill`/`unfill` return new arrays.

export function slotsFromAffordance(affordance) {
  const requirements = (affordance && affordance.requirements) || [];
  return requirements.map((req, index) => ({
    index,
    requirementIndex: index,
    label: req.mode ? `Mode ${req.mode}: ${req.phrase}` : req.phrase,
    count: req.unbounded ? null : req.count,
    minimum: req.minimum || 0,
    mode: req.mode || null,
    targets: [],
  }));
}

function hasRoom(slot) {
  return slot.count === null || slot.targets.length < slot.count;
}

export function activeSlotIndex(slots) {
  const needy = slots.findIndex((s) => s.targets.length < s.minimum);
  if (needy >= 0) return needy;
  const bounded = slots.findIndex((s) => s.count !== null && hasRoom(s));
  if (bounded >= 0) return bounded;
  return slots.findIndex((s) => s.count === null);
}

export function candidateIds(affordance, slot) {
  const ids = new Set();
  for (const c of (affordance && affordance.candidates) || []) {
    if ((c.requirements || []).includes(slot.requirementIndex)) ids.add(c.id);
  }
  return ids;
}

export function canFill(affordance, slot, id) {
  if (!hasRoom(slot)) return false;
  if (slot.targets.includes(id)) return false;
  return candidateIds(affordance, slot).has(id);
}

export function fill(slots, slotIndex, id) {
  const slot = slots[slotIndex];
  if (!slot || !hasRoom(slot) || slot.targets.includes(id)) return slots;
  return slots.map((s, i) => (i === slotIndex ? { ...s, targets: [...s.targets, id] } : s));
}

export function unfill(slots, slotIndex, id) {
  return slots.map((s, i) =>
    i === slotIndex ? { ...s, targets: s.targets.filter((t) => t !== id) } : s
  );
}

export function flatten(slots) {
  return slots.flatMap((s) => s.targets);
}

export function isSatisfied(slots) {
  return slots.every((s) => s.targets.length >= s.minimum);
}

export function sharesValid(shares, amount) {
  if (!Array.isArray(shares) || shares.some((n) => !Number.isInteger(n) || n < 1)) return false;
  return shares.reduce((a, b) => a + b, 0) === amount;
}
```

Note `canFill` refuses the same id twice in one slot but `fill` for a different slot accepts it: that is the CR 115.3 reading the test pins.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_ui_targeting.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/ui_node.py tests/test_ui_targeting.py ui/targeting.js && git commit -m "Model targets as per-requirement slots"
```

### Task 9: `mana.js`

**Files:**
- Create: `ui/mana.js`
- Test: `tests/test_ui_mana.py`

**Interfaces:**
- Produces:
  - `untappedSources(state) -> string[]`: ids of battlefield cards with `controller === state.you`, `tapped === false`, and at least one ability with `is_mana_ability`.
  - `manaSources(pinned, untapped) -> Array<string | {card_id, colors}>`: pinned entries first as `{card_id, colors: [color]}`, then every untapped id not pinned.
  - `pin(pinned, id, color) -> pinned'`, `unpin(pinned, id) -> pinned'`, where `pinned = Array<{card_id, color}>`.
  - `colorOptions(card, abilityIndex) -> string[]` from `card.abilities[i].mana_options` (each option's colours flattened and de-duplicated), or `[]`.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

MOUNTAIN = {"id": "m1", "controller": "alice", "tapped": False, "zone": "battlefield",
            "abilities": [{"index": 0, "text": "{T}: Add {R}.", "is_mana_ability": True, "mana_options": None}]}
TAPPED = {**MOUNTAIN, "id": "m2", "tapped": True}
DUAL = {"id": "d1", "controller": "alice", "tapped": False, "zone": "battlefield",
        "abilities": [{"index": 0, "text": "{T}: Add {R} or {W}.", "is_mana_ability": True, "mana_options": [["R"], ["W"]]}]}
THEIRS = {**MOUNTAIN, "id": "m3", "controller": "bob"}
GRIZZLY = {"id": "c1", "controller": "alice", "tapped": False, "zone": "battlefield",
           "abilities": []}
STATE = {"you": "alice", "battlefield": [MOUNTAIN, TAPPED, DUAL, THEIRS, GRIZZLY]}


def test_untapped_sources_are_yours_untapped_and_mana_producing() -> None:
    assert run_module("mana.js", "m.untappedSources(state)", state=STATE) == ["m1", "d1"]


def test_pinned_sources_go_first_with_their_colour() -> None:
    result = run_module(
        "mana.js",
        "m.manaSources(m.pin([], 'd1', 'W'), m.untappedSources(state))",
        state=STATE,
    )
    assert result == [{"card_id": "d1", "colors": ["W"]}, "m1"]


def test_unpin_restores_the_bare_id() -> None:
    result = run_module("mana.js", "m.manaSources(m.unpin(m.pin([], 'd1', 'W'), 'd1'), ['m1', 'd1'])")
    assert result == ["m1", "d1"]


def test_colour_options_come_from_the_ability() -> None:
    assert run_module("mana.js", "[m.colorOptions(dual, 0), m.colorOptions(mountain, 0)]", dual=DUAL, mountain=MOUNTAIN) == [["R", "W"], []]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ui_mana.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `ui/mana.js`**

```js
// Mana payment from the browser's side. The default is the same as the MCP
// and CLI seats: offer every untapped mana source and let the engine's
// `auto_tap` (CR 601.2g) choose. A pinned source is the player's explicit
// choice - Sunbaked Canyon for {R}, not {W} - and goes first as a
// `ManaSource` with a colour (schemas.ManaSource).

export function untappedSources(state) {
  const you = state.you;
  return (state.battlefield || [])
    .filter(
      (card) =>
        card.controller === you &&
        !card.tapped &&
        (card.abilities || []).some((a) => a.is_mana_ability)
    )
    .map((card) => card.id);
}

export function pin(pinned, id, color) {
  return [...pinned.filter((p) => p.card_id !== id), { card_id: id, color }];
}

export function unpin(pinned, id) {
  return pinned.filter((p) => p.card_id !== id);
}

export function manaSources(pinned, untapped) {
  const pinnedIds = new Set(pinned.map((p) => p.card_id));
  return [
    ...pinned.map((p) => ({ card_id: p.card_id, colors: [p.color] })),
    ...untapped.filter((id) => !pinnedIds.has(id)),
  ];
}

export function colorOptions(card, abilityIndex) {
  const ability = ((card && card.abilities) || [])[abilityIndex];
  const options = (ability && ability.mana_options) || [];
  const seen = [];
  for (const option of options) for (const color of option) if (!seen.includes(color)) seen.push(color);
  return seen;
}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_mana.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ui_mana.py ui/mana.js && git commit -m "Auto-pay by default, pin a source to override"
```

### Task 10: `priority.js` (stop predicate, verbs, stops)

**Files:**
- Create: `ui/priority.js`
- Test: `tests/test_ui_priority.py`; rewrite `tests/test_ui_autopilot.py`, `tests/test_ui_autopilot_attackers.py`, `tests/test_ui_autopilot_blockers.py`, `tests/test_ui_autopilot_next_occurrence.py`, `tests/test_ui_priority_panel.py` against it.

**Interfaces:**
- Consumes: the old `autopilotStopReason(state, run, settings)` and `interpretPassOutcome(res)` in `ui/app.js:1486-1640` - read them in full before starting; their behaviour is pinned by 74 tests, which are being ported, not dropped.
- Produces:
  - `STEPS` (ordered step names as in the old `STEPS` table at `ui/app.js:47-60`) and `PHASE_OF`.
  - `VERBS = ["pass", "pass_turn", "pass_turn_regardless", "until_my_turn", "until_castable"]`.
  - `DEFAULT_STOPS = {mine: Set-like object of steps, theirs: ...}` as `{mine: ["precombat_main", "declare_attackers", "postcombat_main"], theirs: ["declare_attackers", "end"]}` (use the exact step names from `STEPS`).
  - `loadStops(storage) -> stops`, `saveStops(storage, stops)` with key `mcm.stops.v1`; both wrap storage access in try/catch.
  - `startRun(verb, state) -> run` where `run = {verb, startedTurn, startedStep, startedActive, knownStackIds: string[]}`.
  - `stopReason(state, run, stops, castable: boolean) -> string | null`: the old `autopilotStopReason` logic (attacked, new stack object, decision pending, your own declare-attackers when you can attack, and the old three toggles folded into the verb) plus: `pass` stops after one pass; `pass_turn` stops when the stack is non-empty or the turn changes; `pass_turn_regardless` stops only at decisions, combat stops and turn change; `until_my_turn` stops when `state.active_player === state.you && state.turn_number > run.startedTurn`; `until_castable` stops when `castable` is true; and every verb stops at a step in `stops` for the right side (`mine` when `state.active_player === state.you`).
  - `interpretPassOutcome(res) -> {ok, reason}` moved as is.
  - `passButtonsFor(state) -> string[]`: the verbs to offer now (all five when you hold priority; only `pass` when a decision is pending; empty otherwise).

- [ ] **Step 1: Read the old code**

Run: `sed -n 1331,1360p ui/app.js; sed -n 1486,1645p ui/app.js` and read `tests/test_ui_autopilot.py` to see each stop condition's name and rationale. Keep the exact reason strings the old tests assert on where the condition survives.

- [ ] **Step 2: Write the new tests**

`tests/test_ui_priority.py`:

```python
from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node


def board(**over):
    state = {"you": "alice", "priority_holder": "alice", "active_player": "alice", "turn_number": 3,
             "step": "precombat_main", "stack": [], "pending_decision": None, "combat": {"attacks": []},
             "battlefield": [], "hands": {"alice": []}, "winner": None, "drawn": None}
    state.update(over)
    return state


def test_default_stops_are_mtgo_like() -> None:
    assert run_module("priority.js", "m.DEFAULT_STOPS") == {
        "mine": ["precombat_main", "declare_attackers", "postcombat_main"],
        "theirs": ["declare_attackers", "end"],
    }


def test_stops_round_trip_through_storage() -> None:
    result = run_module(
        "priority.js",
        "(() => { const store = new Map(); const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) }; m.saveStops(storage, { mine: ['upkeep'], theirs: [] }); return m.loadStops(storage); })()",
    )
    assert result == {"mine": ["upkeep"], "theirs": []}


def test_a_broken_storage_yields_defaults() -> None:
    result = run_module("priority.js", "m.loadStops({ getItem() { throw new Error('nope'); } })")
    assert result == run_module("priority.js", "m.DEFAULT_STOPS")


def test_pass_turn_stops_when_something_hits_the_stack() -> None:
    reason = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_turn', start), m.DEFAULT_STOPS, false)",
        start=board(), later=board(step="beginning_of_combat", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]),
    )
    assert reason


def test_pass_turn_regardless_ignores_the_stack_but_not_a_decision() -> None:
    quiet = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_turn_regardless', start), { mine: [], theirs: [] }, false)",
        start=board(), later=board(step="beginning_of_combat", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]),
    )
    assert quiet is None
    asked = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_turn_regardless', start), { mine: [], theirs: [] }, false)",
        start=board(), later=board(pending_decision={"player": "alice", "kind": "may", "prompt": "x"}),
    )
    assert asked


def test_until_my_turn_runs_through_the_opponents_turn() -> None:
    expr = "m.stopReason(later, m.startRun('until_my_turn', start), { mine: [], theirs: [] }, false)"
    assert run_module("priority.js", expr, start=board(), later=board(active_player="bob", priority_holder="alice", step="upkeep", turn_number=4)) is None
    assert run_module("priority.js", expr, start=board(), later=board(turn_number=5, step="upkeep"))


def test_until_castable_stops_when_told_something_is_castable() -> None:
    expr = "m.stopReason(later, m.startRun('until_castable', start), { mine: [], theirs: [] }, castable)"
    assert run_module("priority.js", expr, start=board(), later=board(step="end"), castable=False) is None
    assert run_module("priority.js", expr, start=board(), later=board(step="end"), castable=True)


def test_a_stop_halts_every_verb_on_the_right_side() -> None:
    expr = "m.stopReason(later, m.startRun('until_my_turn', start), { mine: [], theirs: ['end'] }, false)"
    assert run_module("priority.js", expr, start=board(), later=board(active_player="bob", turn_number=4, step="end"))
    assert run_module("priority.js", expr, start=board(), later=board(active_player="bob", turn_number=4, step="upkeep")) is None


def test_being_attacked_always_stops() -> None:
    expr = "m.stopReason(later, m.startRun('pass_turn_regardless', start), { mine: [], theirs: [] }, false)"
    later = board(active_player="bob", turn_number=4, step="declare_blockers",
                  combat={"attacks": [{"attacker": "c1", "defender": "alice", "blockers": []}]})
    assert run_module("priority.js", expr, start=board(), later=later)


def test_pass_buttons() -> None:
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board()) == ["pass", "pass_turn", "pass_turn_regardless", "until_my_turn", "until_castable"]
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board(priority_holder="bob")) == []
```

Then port the five old files. For each, keep every test's name and docstring, change `require(APP_JS)` to `run_module("priority.js", ...)`, and map: `autopilotStopReason(state, run, settings)` becomes `stopReason(state, run, stops, castable)` where the old `settings.stopOnStack=false` maps to verb `pass_turn_regardless`, `stopWhenAttacked` is always on, `stopWhenYouCanAttack` is expressed by having `declare_attackers` in `stops.mine`; the old `autopilotTarget` (a step to run to) is expressed as `stops` containing exactly that step. `test_ui_priority_panel.py` asserted HTML of `renderActionBar`; rewrite its three boards against `passButtonsFor`: empty stack and your priority now offers all five verbs (the old rule hid the button on an empty stack because the autopilot picker covered it; the verbs replace the picker), opponent's priority offers none, full stack and your priority offers all five. Keep the `resolve_top` never-offered tests by asserting `"resolve" not in m.VERBS`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_ui_priority.py -v`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement `ui/priority.js`**

```js
// Priority automation. MTGO's yield tiers as verbs, plus a stops panel; no
// key bindings, those vary by client version. Every verb still stops when
// you are attacked, when a decision is pending, when a new object appears on
// the stack (except `pass_turn_regardless`), and at a checked stop.

export const STEPS = [
  "untap", "upkeep", "draw", "precombat_main", "beginning_of_combat",
  "declare_attackers", "declare_blockers", "combat_damage", "end_of_combat",
  "postcombat_main", "end", "cleanup",
];

export const PHASE_OF = {
  untap: "beginning", upkeep: "beginning", draw: "beginning",
  precombat_main: "precombat main", beginning_of_combat: "combat",
  declare_attackers: "combat", declare_blockers: "combat", combat_damage: "combat",
  end_of_combat: "combat", postcombat_main: "postcombat main", end: "ending", cleanup: "ending",
};

export const VERBS = ["pass", "pass_turn", "pass_turn_regardless", "until_my_turn", "until_castable"];

export const VERB_LABELS = {
  pass: "Pass",
  pass_turn: "Pass turn",
  pass_turn_regardless: "Pass turn (even with a stack)",
  until_my_turn: "Pass until my next turn",
  until_castable: "Pass until I can cast something",
};

export const DEFAULT_STOPS = {
  mine: ["precombat_main", "declare_attackers", "postcombat_main"],
  theirs: ["declare_attackers", "end"],
};

const STOPS_KEY = "mcm.stops.v1";

export function loadStops(storage) {
  try {
    const raw = storage.getItem(STOPS_KEY);
    if (!raw) return { mine: [...DEFAULT_STOPS.mine], theirs: [...DEFAULT_STOPS.theirs] };
    const parsed = JSON.parse(raw);
    return { mine: parsed.mine || [], theirs: parsed.theirs || [] };
  } catch {
    return { mine: [...DEFAULT_STOPS.mine], theirs: [...DEFAULT_STOPS.theirs] };
  }
}

export function saveStops(storage, stops) {
  try {
    storage.setItem(STOPS_KEY, JSON.stringify(stops));
  } catch {
    // storage unavailable: the stops live for this page only
  }
}

export function startRun(verb, state) {
  return {
    verb,
    startedTurn: state.turn_number,
    startedStep: state.step,
    startedActive: state.active_player,
    knownStackIds: (state.stack || []).map((o) => o.id),
    passes: 0,
  };
}

function attackedNow(state) {
  return ((state.combat && state.combat.attacks) || []).some((a) => a.defender === state.you);
}

export function stopReason(state, run, stops, castable) {
  if (state.winner || state.drawn) return "the game is over";
  if (state.pending_decision && state.pending_decision.player === state.you) return "a decision is pending";
  if (attackedNow(state)) return "you are being attacked";
  const stackIds = (state.stack || []).map((o) => o.id);
  const newObject = stackIds.some((id) => !run.knownStackIds.includes(id));
  const mine = state.active_player === state.you;
  const side = mine ? stops.mine : stops.theirs;
  const turnChanged = state.turn_number !== run.startedTurn;
  switch (run.verb) {
    case "pass":
      return run.passes >= 1 ? "passed once" : null;
    case "pass_turn":
      if (newObject) return "something new is on the stack";
      if (turnChanged) return "the turn ended";
      break;
    case "pass_turn_regardless":
      if (turnChanged) return "the turn ended";
      break;
    case "until_my_turn":
      if (newObject) return "something new is on the stack";
      if (mine && turnChanged) return "it is your turn";
      break;
    case "until_castable":
      if (newObject) return "something new is on the stack";
      if (castable) return "you can cast something";
      break;
    default:
      return `unknown verb ${run.verb}`;
  }
  if (side.includes(state.step) && !(state.step === run.startedStep && !turnChanged && state.active_player === run.startedActive)) {
    return `stop at ${mine ? "your" : "their"} ${state.step.replace(/_/g, " ")}`;
  }
  return null;
}

export function interpretPassOutcome(res) {
  if (!res || res.allowed === false) return { ok: false, reason: (res && res.reason) || "pass refused" };
  return { ok: true, reason: null, outcome: res.outcome || null, settledThrough: res.settled_through || null };
}

export function passButtonsFor(state) {
  if (state.priority_holder !== state.you) return [];
  if (state.pending_decision && state.pending_decision.player === state.you) return ["pass"];
  return [...VERBS];
}
```

The `stop at ...` guard skips the stop the run started at, so "Pass turn" from your precombat main does not halt immediately on your own precombat main stop.

- [ ] **Step 5: Run all priority tests**

Run: `uv run pytest tests/test_ui_priority.py tests/test_ui_autopilot*.py tests/test_ui_priority_panel.py -v`
Expected: PASS. Where a ported old test disagrees with the verb model and the old behaviour is not a rule, rewrite the assertion to the verb model and say so in the test's docstring; where it is a rule (attacked, decision pending, new stack object), the new code must satisfy it.

- [ ] **Step 6: Commit**

```bash
git add ui/priority.js tests/test_ui_priority.py tests/test_ui_autopilot*.py tests/test_ui_priority_panel.py && git commit -m "Tiered pass verbs and persisted stops"
```

### Task 11: `actions.js` (the one action state machine)

**Files:**
- Create: `ui/actions.js`
- Test: `tests/test_ui_actions.py`; rewrite `tests/test_ui_targets.py` against `bodyFor`.

**Interfaces:**
- Consumes: `targeting.js` (`slotsFromAffordance`, `fill`, `flatten`, `isSatisfied`, `sharesValid`), `mana.js` (`manaSources`).
- Produces:
  - `needsChoices(card, abilityIndex) -> boolean`: true when the cast (or ability) has modes, targets, X, alternative costs or object costs to choose. Reads `card.cast.modes.options.length`, `card.cast.targeting.targets_anything`, `card.cast.needs_x`, and for an ability `card.abilities[i].targeting.targets_anything`, `.needs_x`, `.mana_options`.
  - `begin(kind, card, extra) -> Action` with `kind` one of `"cast" | "activate" | "special"`, `Action = {kind, cardId, card, abilityIndex, keyword, modes: number[], xValue: number|null, color, altCost, choosing: string[], convoke: string[], delve: string[], slots: Slot[], shares: Record<string, number>, pinned: [], affordance: null, error: null, holdPriority: false}`.
  - `withAffordance(action, affordance) -> Action`: rebuilds `slots` from the affordance keeping targets that are still candidates for their slot.
  - `toggleMode(action, n) -> Action`, `setX(action, n) -> Action`, `chooseColor(action, c)`, `chooseAltCost(action, indexOrNull)`, `togglePitch(action, id)`, `toggleConvoke(action, id)`, `toggleDelve(action, id)`, `chooseTarget(action, id) -> Action` (fills the active slot, or unfills if already there), `setShare(action, id, n)`, `pinSource(action, id, color)`, `unpinSource(action, id)`.
  - `affordanceQuery(action) -> {ability_index?, modes?, x_value?}` for the GET.
  - `modesReady(action) -> boolean` (chosen count within `card.cast.modes.minimum..maximum`).
  - `readyToFire(action) -> boolean`: modes ready, X set when needed, slots satisfied, shares valid when a division exists, and every "up to"/unbounded slot is either empty-satisfied or the action carries `done: true`.
  - `bodyFor(action, you, untapped) -> object`: the request body. Targets: `undefined` when the card does not target, `[]` when it targets optionally and none chosen, `[...]` otherwise (this is the old `sendTargets` three-way rule, now over slots).
  - `endpointFor(action) -> string` (`actions/cast`, `actions/activate`, or `actions/<keyword>` for specials).

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

BOLT = {"id": "b1", "name": "Lightning Bolt", "zone": "hand", "controller": "alice", "type_line": "Instant",
        "cast": {"mana_cost": "{R}", "needs_x": False, "modes": None,
                 "targeting": {"targets_anything": True, "understood": True, "minimum": 1, "maximum": 1,
                               "requirements": [{"phrase": "any target", "count": 1, "minimum": 1, "unbounded": False, "variable_count": False, "mana": None, "mode": None, "mode_text": None}]}},
        "abilities": []}
MEMNITE = {"id": "mm", "name": "Memnite", "zone": "hand", "controller": "alice", "type_line": "Artifact Creature",
           "cast": {"mana_cost": "{0}", "needs_x": False, "modes": None,
                    "targeting": {"targets_anything": False, "understood": True, "minimum": 0, "maximum": 0, "requirements": []}},
           "abilities": []}
KOZILEK = {"id": "kc", "name": "Kozilek's Command", "zone": "hand", "controller": "alice", "type_line": "Kindred Instant",
           "cast": {"mana_cost": "{X}{C}{C}", "needs_x": True,
                    "modes": {"instruction": "Choose two -", "minimum": 2, "maximum": 2, "repeatable": False, "understood": True,
                              "options": [{"number": 1, "text": "Target player creates X ..."}, {"number": 2, "text": "Target player scries X, then draws a card."},
                                          {"number": 3, "text": "Exile target creature with mana value X or less."}, {"number": 4, "text": "Exile up to X target cards from graveyards."}]},
                    "targeting": {"targets_anything": True, "understood": False, "minimum": 0, "maximum": None, "requirements": []}},
           "abilities": []}
BOLT_AFF = {"targets_anything": True, "understood": True, "minimum": 1, "maximum": 1,
            "requirements": [{"phrase": "any target", "count": 1, "minimum": 1, "unbounded": False, "variable_count": False, "mode": None, "mode_text": None}],
            "division": None,
            "candidates": [{"id": "bob", "kind": "player", "name": "bob", "requirements": [0]}, {"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]}]}
KOZ_AFF = {"targets_anything": True, "understood": True, "minimum": 2, "maximum": 2,
           "requirements": [{"phrase": "target player", "count": 1, "minimum": 1, "unbounded": False, "variable_count": False, "mode": 1, "mode_text": "Target player creates X ..."},
                            {"phrase": "target player", "count": 1, "minimum": 1, "unbounded": False, "variable_count": False, "mode": 2, "mode_text": "Target player scries X, then draws a card."}],
           "division": None,
           "candidates": [{"id": "alice", "kind": "player", "name": "alice", "requirements": [0, 1]}, {"id": "bob", "kind": "player", "name": "bob", "requirements": [0, 1]}]}


def test_memnite_needs_no_choices_and_bolt_does() -> None:
    assert run_module("actions.js", "[m.needsChoices(memnite, null), m.needsChoices(bolt, null)]", memnite=MEMNITE, bolt=BOLT) == [False, True]


def test_bolt_body_carries_target_and_all_sources() -> None:
    body = run_module(
        "actions.js",
        "(() => { let a = m.begin('cast', bolt, {}); a = m.withAffordance(a, aff); a = m.chooseTarget(a, 'bob'); return [m.readyToFire(a), m.bodyFor(a, 'alice', ['m1', 'm2'])]; })()",
        bolt=BOLT, aff=BOLT_AFF,
    )
    assert body == [True, {"player": "alice", "card_id": "b1", "targets": ["bob"], "mana_sources": ["m1", "m2"]}]


def test_kozilek_flow_same_player_twice() -> None:
    result = run_module(
        "actions.js",
        """(() => {
          let a = m.begin('cast', koz, {});
          const before = m.affordanceQuery(a);
          a = m.toggleMode(a, 1); a = m.toggleMode(a, 2); a = m.setX(a, 1);
          const q = m.affordanceQuery(a);
          a = m.withAffordance(a, aff);
          a = m.chooseTarget(a, 'alice'); a = m.chooseTarget(a, 'alice');
          return [before, q, m.readyToFire(a), m.bodyFor(a, 'alice', ['w1', 'w2', 'w3'])];
        })()""",
        koz=KOZILEK, aff=KOZ_AFF,
    )
    assert result[0] == {}
    assert result[1] == {"modes": [1, 2], "x_value": 1}
    assert result[2] is True
    assert result[3] == {"player": "alice", "card_id": "kc", "targets": ["alice", "alice"], "modes": [1, 2], "x_value": 1, "mana_sources": ["w1", "w2", "w3"]}


def test_choosing_a_target_again_unfills_it() -> None:
    targets = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', bolt, {}), aff); a = m.chooseTarget(a, 'bob'); a = m.chooseTarget(a, 'bob'); return m.bodyFor(a, 'alice', []).targets; })()",
        bolt=BOLT, aff=BOLT_AFF,
    )
    assert targets == []


def test_modes_are_not_ready_until_enough_are_chosen() -> None:
    assert run_module("actions.js", "[m.modesReady(m.begin('cast', koz, {})), m.modesReady(m.toggleMode(m.toggleMode(m.begin('cast', koz, {}), 1), 3))]", koz=KOZILEK) == [False, True]


def test_pinned_source_leads_the_mana_sources() -> None:
    body = run_module(
        "actions.js",
        "m.bodyFor(m.pinSource(m.withAffordance(m.chooseTarget(m.begin('cast', bolt, {}), 'bob'), aff), 'd1', 'R'), 'alice', ['d1', 'm1'])",
        bolt=BOLT, aff=BOLT_AFF,
    )
    assert body["mana_sources"] == [{"card_id": "d1", "colors": ["R"]}, "m1"]


def test_activate_body_and_endpoint() -> None:
    land = {"id": "d1", "name": "Sunbaked Canyon", "zone": "battlefield", "controller": "alice",
            "abilities": [{"index": 0, "text": "{T}, Pay 1 life: Add {R} or {W}.", "is_mana_ability": True, "needs_x": False, "mana_options": [["R"], ["W"]],
                           "targeting": {"targets_anything": False, "understood": True, "minimum": 0, "maximum": 0, "requirements": []}}]}
    result = run_module(
        "actions.js",
        "(() => { let a = m.begin('activate', land, { abilityIndex: 0 }); a = m.chooseColor(a, 'W'); return [m.endpointFor(a), m.bodyFor(a, 'alice', ['d1']), m.readyToFire(a)]; })()",
        land=land,
    )
    assert result == ["actions/activate", {"player": "alice", "card_id": "d1", "ability_index": 0, "color": "W"}, True]
```

Note: `chooseTarget` in the second-to-last test is called before `withAffordance`; with no slots yet it must be a no-op, and `withAffordance` then builds the slots. Adjust that test to call `withAffordance` first if you prefer the stricter reading, but keep the no-op behaviour.

Port `tests/test_ui_targets.py`: its eight cases pin `sendTargets` returning `undefined` / `[]` / `[...]`; rewrite each as `m.bodyFor(action, 'alice', []).targets` with the action built from a card whose `cast.targeting` matches the case (non-targeting card: key absent; optional targets none chosen: `[]`; chosen: list; unreadable `understood: false` with clicked ids: the list).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ui_actions.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `ui/actions.js`**

```js
// The one in-flight action: cast, activate, or a special action. Pure state
// transitions; `render/ribbon.js` draws it and `app.js` fires it.
import { activeSlotIndex, fill, flatten, isSatisfied, sharesValid, slotsFromAffordance, unfill } from "./targeting.js";
import { manaSources, pin, unpin } from "./mana.js";

function castHints(card) {
  return (card && card.cast) || {};
}

function abilityOf(card, abilityIndex) {
  return abilityIndex === null || abilityIndex === undefined ? null : ((card && card.abilities) || [])[abilityIndex] || null;
}

export function needsChoices(card, abilityIndex) {
  const ability = abilityOf(card, abilityIndex);
  if (ability) {
    return Boolean(
      (ability.targeting && ability.targeting.targets_anything) ||
        ability.needs_x ||
        (ability.mana_options && ability.mana_options.length > 1)
    );
  }
  const hints = castHints(card);
  const modes = hints.modes && hints.modes.options && hints.modes.options.length;
  return Boolean(modes || (hints.targeting && hints.targeting.targets_anything) || hints.needs_x);
}

export function begin(kind, card, extra) {
  extra = extra || {};
  return {
    kind,
    cardId: card.id,
    card,
    abilityIndex: extra.abilityIndex === undefined ? null : extra.abilityIndex,
    keyword: extra.keyword || null,
    modes: [],
    xValue: null,
    color: null,
    altCost: null,
    choosing: [],
    convoke: [],
    delve: [],
    slots: [],
    shares: {},
    pinned: [],
    affordance: null,
    error: null,
    holdPriority: false,
    done: false,
  };
}

export function withAffordance(action, affordance) {
  const fresh = slotsFromAffordance(affordance);
  const kept = fresh.map((slot, i) => {
    const old = action.slots[i];
    if (!old) return slot;
    const legal = new Set((affordance.candidates || []).filter((c) => (c.requirements || []).includes(slot.requirementIndex)).map((c) => c.id));
    return { ...slot, targets: old.targets.filter((id) => legal.has(id)) };
  });
  return { ...action, affordance, slots: kept, error: affordance && affordance.error ? affordance.error : null };
}

export function toggleMode(action, n) {
  const modes = action.modes.includes(n) ? action.modes.filter((x) => x !== n) : [...action.modes, n];
  return { ...action, modes: modes.sort((a, b) => a - b), slots: [], affordance: null };
}

export function setX(action, n) {
  return { ...action, xValue: n === null || n === undefined || Number.isNaN(n) ? null : Number(n), slots: [], affordance: null };
}

export function chooseColor(action, color) {
  return { ...action, color };
}

export function chooseAltCost(action, index) {
  return { ...action, altCost: index === undefined ? null : index, choosing: [] };
}

function toggleIn(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

export function togglePitch(action, id) {
  return { ...action, choosing: toggleIn(action.choosing, id) };
}

export function toggleConvoke(action, id) {
  return { ...action, convoke: toggleIn(action.convoke, id) };
}

export function toggleDelve(action, id) {
  return { ...action, delve: toggleIn(action.delve, id) };
}

export function chooseTarget(action, id) {
  const already = action.slots.findIndex((s) => s.targets.includes(id));
  if (already >= 0) {
    // A second click on a chosen object unfills it from the LAST slot it sits
    // in, so a player who put "alice" in mode 1 and mode 2 and clicks once
    // removes only the mode 2 one.
    const last = action.slots.map((s) => s.targets.includes(id)).lastIndexOf(true);
    const active = activeSlotIndex(action.slots);
    if (active >= 0 && action.affordance && canTake(action, active, id)) {
      return { ...action, slots: fill(action.slots, active, id) };
    }
    return { ...action, slots: unfill(action.slots, last, id) };
  }
  const active = activeSlotIndex(action.slots);
  if (active < 0 || !canTake(action, active, id)) return action;
  return { ...action, slots: fill(action.slots, active, id) };
}

function canTake(action, slotIndex, id) {
  const slot = action.slots[slotIndex];
  const candidates = (action.affordance && action.affordance.candidates) || [];
  return candidates.some((c) => c.id === id && (c.requirements || []).includes(slot.requirementIndex));
}

export function setShare(action, id, n) {
  return { ...action, shares: { ...action.shares, [id]: Number(n) } };
}

export function pinSource(action, id, color) {
  return { ...action, pinned: pin(action.pinned, id, color) };
}

export function unpinSource(action, id) {
  return { ...action, pinned: unpin(action.pinned, id) };
}

export function markDone(action) {
  return { ...action, done: true };
}

export function affordanceQuery(action) {
  const query = {};
  if (action.abilityIndex !== null) query.ability_index = action.abilityIndex;
  if (action.modes.length) query.modes = action.modes;
  if (action.xValue !== null) query.x_value = action.xValue;
  return query;
}

export function modesReady(action) {
  const modes = castHints(action.card).modes;
  if (!modes || !modes.options || !modes.options.length || action.abilityIndex !== null) return true;
  return action.modes.length >= modes.minimum && action.modes.length <= modes.maximum;
}

function needsX(action) {
  const ability = abilityOf(action.card, action.abilityIndex);
  return ability ? Boolean(ability.needs_x) : Boolean(castHints(action.card).needs_x);
}

function targeting(action) {
  if (action.affordance) return action.affordance;
  const ability = abilityOf(action.card, action.abilityIndex);
  return ability ? ability.targeting || {} : castHints(action.card).targeting || {};
}

export function readyToFire(action) {
  if (!modesReady(action)) return false;
  if (needsX(action) && action.xValue === null) return false;
  const profile = targeting(action);
  if (profile.targets_anything) {
    if (!action.affordance) return false;
    if (!isSatisfied(action.slots)) return false;
    const open = action.slots.some((s) => (s.count === null || s.minimum < (s.count || 0)) && s.targets.length < (s.count === null ? Infinity : s.count));
    if (open && !action.done) return false;
    if (action.affordance.division) {
      const targets = flatten(action.slots);
      if (!sharesValid(targets.map((id) => action.shares[id]), action.affordance.division.amount)) return false;
    }
  }
  return true;
}

export function endpointFor(action) {
  if (action.kind === "activate") return "actions/activate";
  if (action.kind === "special") return `actions/${action.keyword.toLowerCase()}`;
  return "actions/cast";
}

export function bodyFor(action, you, untapped) {
  const body = { player: you, card_id: action.cardId };
  if (action.kind === "special") return body;
  if (action.kind === "activate") {
    body.ability_index = action.abilityIndex;
    if (action.color) body.color = action.color;
  }
  const profile = targeting(action);
  if (profile.targets_anything) {
    body.targets = flatten(action.slots);
  } else if (profile.understood === false && action.slots.length === 0 && action.freeTargets && action.freeTargets.length) {
    body.targets = [...action.freeTargets];
  }
  if (action.kind === "cast") {
    if (action.modes.length) body.modes = action.modes;
    if (action.xValue !== null) body.x_value = action.xValue;
    if (action.keyword) body.keyword = action.keyword;
    if (action.convoke.length) body.convoke = action.convoke.map((id) => ({ card_id: id }));
    if (action.delve.length) body.delve = action.delve;
    if (action.altCost !== null) body.alternative_cost = action.altCost;
    if (action.choosing.length) body.choosing = action.choosing;
    if (action.affordance && action.affordance.division) body.shares = flatten(action.slots).map((id) => action.shares[id]);
  } else if (action.xValue !== null) {
    body.x_value = action.xValue;
  }
  const sources = manaSources(action.pinned, untapped || []);
  if (sources.length && !(action.kind === "activate" && action.pinned.length === 0 && (untapped || []).length <= 1 && abilityOf(action.card, action.abilityIndex) && abilityOf(action.card, action.abilityIndex).is_mana_ability)) {
    body.mana_sources = sources;
  }
  return body;
}
```

The last condition keeps a mana ability's own activation from offering itself as a source; simplify it to `if (sources.length && !(isManaAbility(action)))` with a small `isManaAbility` helper, which is what the `test_activate_body_and_endpoint` case pins (no `mana_sources` key). `freeTargets` is the unreadable-targeting escape hatch: `render/ribbon.js` appends clicked ids to it when `affordance.understood === false`; add `export function addFreeTarget(action, id)` that toggles the id in `action.freeTargets` (initialise `freeTargets: []` in `begin`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_actions.py tests/test_ui_targets.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/actions.js tests/test_ui_actions.py tests/test_ui_targets.py && git commit -m "One action state machine for cast, activate and specials"
```

### Task 12: `decisions.js` and `combat.js` view models

**Files:**
- Create: `ui/decisions.js`, `ui/combat.js`
- Test: `tests/test_ui_decisions.py`, `tests/test_ui_combat.py`; rewrite the renderer halves of `tests/test_card_split_labels.py`, `tests/test_fetchland_search_choice.py`, `tests/test_trigger_order_labels.py`, and `tests/test_ui_decision_coverage.py`.

**Interfaces:**
- Consumes: the old `chooseCardsOptions`, `triggerOrderOptions`, `renderChooseCardsDecision`, `renderCardSplitDecision` at `ui/app.js:891-1130` - read them; their option-labelling rules are pinned by the three label test files.
- Produces (`decisions.js`):
  - `DECISION_KINDS`: the object-literal keys the coverage test extracts, one per kind in `decisions.ANSWERABLE_KINDS` (`may, pay_life, pay_energy, pay_mana, pay_energy_amount, pay_life_amount, scry, surveil, mulligan, opening_hand_action, choose_cards, choose_targets, choose_a_quality, choose_a_mode, put_on_top_or_bottom, order_triggers`), each mapping to a view-model function `(pending, draft) -> ViewModel`.
  - `ViewModel = {kind, title, prompt, controls: Control[], submit: {label, enabled, answer} | null, secondary: {label, answer} | null, remaining: number | null}` where `Control` is one of `{type: "options", items: [{id, label, selected, disabled}]}`, `{type: "number", min, max, value}`, `{type: "targets", slots}` (reusing `targeting.js` slots built from `pending.targets` and `target_minimum`/`target_maximum`), `{type: "split", cards: [{id, label, moved}], movedLabel, keptLabel, order: string[]}`, `{type: "order", items}`.
  - `chooseCardsOptions(pending)`, `triggerOrderOptions(pending)` moved verbatim (same names, same output shape).
  - `answerFor(kind, draft) -> body` producing exactly the `DecideRequest` fields the old `submit*` functions sent (`card_ids`, `targets`, `order` reversed as before, `accept`, `energy_amount`, `life_amount`, `choice`, `top`, `moved` + `top_order`).
- Produces (`combat.js`):
  - `attackCandidates(state) -> Card[]` (your untapped creatures without summoning sickness, as the old `openAttackModal` filtered; the engine still decides).
  - `defenders(state) -> [{id, label}]` (opponent player plus their planeswalkers, and battles if `type_line` includes "Battle").
  - `toggleAttacker(draft, cardId, defenderId) -> draft`, `attackBody(draft) -> {attackers: [{card_id, defender}]}`.
  - `blockCandidates(state) -> Card[]`, `attacksAgainstYou(state) -> attacks`, `toggleBlock(draft, blockerId, attackerId) -> draft`, `blockBody(draft, you) -> {player, blocks: [{blocker, blocked}]}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ui_decisions.py`:

```python
from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

MAY = {"player": "alice", "kind": "may", "prompt": "Pay 2 life?", "source": "Shockland", "amount": None, "cards": [],
       "targets": [], "target_minimum": None, "target_maximum": None, "affirmative_is_legal": True, "options": [],
       "moved_label": None, "kept_label": None, "card_labels": [], "exact": False, "default_cards": None}
TARGETS = {**MAY, "kind": "choose_targets", "prompt": "Choose a target", "targets": [{"kind": "player", "id": "bob"}, {"kind": "card", "id": "c1"}],
           "target_minimum": 1, "target_maximum": 1, "card_details": {"c1": {"name": "Memnite"}}}
SCRY = {**MAY, "kind": "scry", "prompt": "Scry 2", "cards": ["a", "b"], "card_labels": ["Island", "Bolt"], "moved_label": "bottom", "kept_label": "top"}


def test_every_answerable_kind_has_a_view_model() -> None:
    kinds = run_module("decisions.js", "Object.keys(m.DECISION_KINDS).sort()")
    assert kinds == sorted(["may", "pay_life", "pay_energy", "pay_mana", "pay_energy_amount", "pay_life_amount", "scry", "surveil",
                            "mulligan", "opening_hand_action", "choose_cards", "choose_targets", "choose_a_quality", "choose_a_mode",
                            "put_on_top_or_bottom", "order_triggers"])


def test_may_offers_yes_and_no() -> None:
    vm = run_module("decisions.js", "m.viewModel(p, {})", p=MAY)
    assert vm["submit"]["answer"] == {"accept": True}
    assert vm["secondary"]["answer"] == {"accept": False}


def test_choose_targets_uses_slots_with_the_minimum() -> None:
    vm = run_module("decisions.js", "m.viewModel(p, { targets: ['bob'] })", p=TARGETS)
    (control,) = [c for c in vm["controls"] if c["type"] == "targets"]
    assert control["slots"][0]["minimum"] == 1
    assert vm["submit"]["enabled"] is True
    assert vm["submit"]["answer"] == {"targets": ["bob"]}
    assert run_module("decisions.js", "m.viewModel(p, {}).submit.enabled", p=TARGETS) is False


def test_scry_answer_carries_moved_and_top_order() -> None:
    answer = run_module("decisions.js", "m.answerFor('scry', { moved: ['a'], order: ['b'] })")
    assert answer == {"moved": ["a"], "top_order": ["b"]}
```

`tests/test_ui_combat.py`:

```python
from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

def creature(id, controller, **over):
    c = {"id": id, "name": id, "controller": controller, "tapped": False, "summoning_sick": False,
         "type_line": "Creature", "zone": "battlefield", "abilities": []}
    c.update(over)
    return c

STATE = {"you": "alice", "active_player": "alice", "step": "declare_attackers",
         "players": [{"username": "alice"}, {"username": "bob"}],
         "battlefield": [creature("a1", "alice"), creature("a2", "alice", tapped=True), creature("a3", "alice", summoning_sick=True),
                         creature("pw", "bob", type_line="Legendary Planeswalker"), creature("b1", "bob")],
         "combat": {"attacks": []}}


def test_attack_candidates_and_defenders() -> None:
    result = run_module("combat.js", "[m.attackCandidates(s).map(c => c.id), m.defenders(s).map(d => d.id)]", s=STATE)
    assert result == [["a1"], ["bob", "pw"]]


def test_attack_body_from_toggles() -> None:
    body = run_module("combat.js", "m.attackBody(m.toggleAttacker(m.toggleAttacker({}, 'a1', 'bob'), 'a3', 'pw'))")
    assert body == {"attackers": [{"card_id": "a1", "defender": "bob"}, {"card_id": "a3", "defender": "pw"}]}
    assert run_module("combat.js", "m.attackBody(m.toggleAttacker(m.toggleAttacker({}, 'a1', 'bob'), 'a1', 'bob'))") == {"attackers": []}


def test_block_body_from_toggles() -> None:
    state = {**STATE, "active_player": "bob", "step": "declare_blockers",
             "combat": {"attacks": [{"attacker": "b1", "defender": "alice", "blockers": []}]}}
    body = run_module("combat.js", "m.blockBody(m.toggleBlock({}, 'a1', 'b1'), 'alice')", s=state)
    assert body == {"player": "alice", "blocks": [{"blocker": "a1", "blocked": "b1"}]}
    assert run_module("combat.js", "m.attacksAgainstYou(s).map(a => a.attacker)", s=state) == ["b1"]
```

Port the label tests: in `test_card_split_labels.py`, `test_fetchland_search_choice.py`, `test_trigger_order_labels.py`, replace the `require(APP_JS)` renderer calls with `run_module("decisions.js", "m.chooseCardsOptions(p)", p=pending)` / `"m.triggerOrderOptions(p)"` / `"m.viewModel(p, {})"` and assert on the returned option labels instead of on HTML substrings (the labels are the same strings the old HTML contained). Rewrite `test_ui_decision_coverage.py` to regex the keys of `DECISION_KINDS` out of `ui/decisions.js` (same regex approach, new file and identifier) and keep its `SERVED_ELSEWHERE` assertions pointing at `combat.js`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ui_decisions.py tests/test_ui_combat.py -v`
Expected: FAIL (modules not found).

- [ ] **Step 3: Implement `ui/decisions.js`**

Move `chooseCardsOptions` and `triggerOrderOptions` from `ui/app.js` unchanged (as `export function`). Then:

```js
import { slotsFromAffordance, fill, isSatisfied, flatten } from "./targeting.js";

function labelOf(pending, id) {
  const details = pending.card_details || {};
  if (details[id] && details[id].name) return details[id].name;
  const i = (pending.cards || []).indexOf(id);
  if (i >= 0 && pending.card_labels && pending.card_labels[i]) return pending.card_labels[i];
  return id;
}

function yesNo(pending, yes, no) {
  return {
    kind: pending.kind, title: pending.source || pending.kind, prompt: pending.prompt, controls: [],
    submit: { label: yes, enabled: pending.affirmative_is_legal !== false, answer: { accept: true } },
    secondary: { label: no, answer: { accept: false } }, remaining: null,
  };
}

function amount(pending, draft, field) {
  const value = draft.amount === undefined ? 0 : draft.amount;
  return {
    kind: pending.kind, title: pending.source || pending.kind, prompt: pending.prompt,
    controls: [{ type: "number", min: 0, max: pending.amount, value }],
    submit: { label: "Pay", enabled: value >= 0 && value <= pending.amount, answer: { [field]: value } },
    secondary: null, remaining: null,
  };
}

function chooseCards(pending, draft) {
  const chosen = draft.card_ids || [];
  const options = chooseCardsOptions(pending).map((o) => ({ ...o, selected: chosen.includes(o.id), disabled: false }));
  const max = pending.amount;
  const ok = pending.exact ? chosen.length === max : chosen.length <= (max === null || max === undefined ? Infinity : max);
  return {
    kind: pending.kind, title: pending.source || "Choose cards", prompt: pending.prompt,
    controls: [{ type: "options", items: options, multi: true }],
    submit: { label: pending.exact ? `Choose ${max}` : "Choose", enabled: ok, answer: { card_ids: chosen } },
    secondary: pending.exact ? null : { label: "Find nothing", answer: { card_ids: [] } }, remaining: null,
  };
}

function targetsAffordance(pending) {
  return {
    targets_anything: true, understood: true, minimum: pending.target_minimum || 0, maximum: pending.target_maximum,
    requirements: [{ phrase: pending.prompt, count: pending.target_maximum === null || pending.target_maximum === undefined ? 1 : pending.target_maximum,
                     minimum: pending.target_minimum || 0, unbounded: pending.target_maximum === null || pending.target_maximum === undefined, variable_count: false, mode: null, mode_text: null }],
    division: null,
    candidates: (pending.targets || []).map((t) => ({ id: t.id, kind: t.kind, name: labelOf(pending, t.id), requirements: [0] })),
  };
}

function chooseTargets(pending, draft) {
  const aff = targetsAffordance(pending);
  let slots = slotsFromAffordance(aff);
  for (const id of draft.targets || []) slots = fill(slots, 0, id);
  return {
    kind: pending.kind, title: pending.source || "Choose targets", prompt: pending.prompt,
    controls: [{ type: "targets", slots, affordance: aff }],
    submit: { label: "Choose", enabled: isSatisfied(slots), answer: { targets: flatten(slots) } },
    secondary: null, remaining: null,
  };
}

function split(pending, draft) {
  const moved = draft.moved || [];
  const kept = (pending.cards || []).filter((id) => !moved.includes(id));
  const order = (draft.order || kept).filter((id) => kept.includes(id));
  return {
    kind: pending.kind, title: pending.source || pending.kind, prompt: pending.prompt,
    controls: [{ type: "split", cards: (pending.cards || []).map((id) => ({ id, label: labelOf(pending, id), moved: moved.includes(id) })),
                 movedLabel: pending.moved_label || (pending.kind === "surveil" ? "graveyard" : "bottom"), keptLabel: pending.kept_label || "top", order }],
    submit: { label: "Done", enabled: true, answer: { moved, top_order: order.length ? order : null } },
    secondary: null, remaining: null,
  };
}

function choice(pending, draft) {
  const items = (pending.options || []).map((o) => ({ id: o, label: o, selected: draft.choice === o, disabled: false }));
  return {
    kind: pending.kind, title: pending.source || pending.kind, prompt: pending.prompt,
    controls: [{ type: "options", items, multi: false }],
    submit: { label: "Choose", enabled: draft.choice !== undefined, answer: { choice: draft.choice } },
    secondary: null, remaining: null,
  };
}

function topOrBottom(pending) {
  return {
    kind: pending.kind, title: pending.source || pending.kind, prompt: pending.prompt, controls: [],
    submit: { label: "Top", enabled: true, answer: { top: true } },
    secondary: { label: "Bottom", answer: { top: false } }, remaining: null,
  };
}

function orderTriggers(pending, draft) {
  const order = draft.order || [];
  const items = triggerOrderOptions(pending).map((o) => ({ ...o, selected: order.includes(o.id), position: order.indexOf(o.id) }));
  const all = items.length === order.length;
  return {
    kind: pending.kind, title: "Order triggers", prompt: pending.prompt,
    controls: [{ type: "order", items }],
    submit: { label: "Put on the stack", enabled: all, answer: { order: [...order].reverse() } },
    secondary: null, remaining: null,
  };
}

export const DECISION_KINDS = {
  may: (p) => yesNo(p, "Yes", "No"),
  pay_life: (p) => yesNo(p, "Pay", "Don't pay"),
  pay_energy: (p) => yesNo(p, "Pay", "Don't pay"),
  pay_mana: (p) => yesNo(p, "Pay", "Don't pay"),
  pay_energy_amount: (p, d) => amount(p, d, "energy_amount"),
  pay_life_amount: (p, d) => amount(p, d, "life_amount"),
  scry: split,
  surveil: split,
  mulligan: (p) => yesNo(p, "Keep", "Mulligan"),
  opening_hand_action: (p) => yesNo(p, "Do it", "Skip"),
  choose_cards: chooseCards,
  choose_targets: chooseTargets,
  choose_a_quality: choice,
  choose_a_mode: choice,
  put_on_top_or_bottom: topOrBottom,
  order_triggers: orderTriggers,
};

export function viewModel(pending, draft) {
  const build = DECISION_KINDS[pending.kind];
  if (!build) return { kind: pending.kind, title: "Unknown decision", prompt: `no renderer for ${pending.kind}`, controls: [], submit: null, secondary: null, remaining: null };
  return build(pending, draft || {});
}

export function answerFor(kind, draft) {
  switch (kind) {
    case "scry":
    case "surveil":
      return { moved: draft.moved || [], top_order: draft.order && draft.order.length ? draft.order : null };
    case "order_triggers":
      return { order: [...(draft.order || [])].reverse() };
    default:
      return viewModel({ kind, ...draft.pending }, draft).submit.answer;
  }
}
```

Check the old `mulligan` and `opening_hand_action` answer fields in `ui/app.js:810-849` and `decisions.py`'s parsers (`bool_answer`) so the yes/no bodies match (`accept`). If `renderChoiceDecision` sent `choice` as the option string, keep it.

Implement `ui/combat.js`:

```js
export function attackCandidates(state) {
  return (state.battlefield || []).filter(
    (c) => c.controller === state.you && !c.tapped && !c.summoning_sick && /creature/i.test(c.type_line || "")
  );
}

export function opponentOf(state) {
  return ((state.players || []).map((p) => p.username).find((u) => u !== state.you)) || null;
}

export function defenders(state) {
  const opponent = opponentOf(state);
  const list = opponent ? [{ id: opponent, label: opponent }] : [];
  for (const c of state.battlefield || []) {
    if (c.controller !== state.you && /planeswalker|battle/i.test(c.type_line || "")) list.push({ id: c.id, label: c.name });
  }
  return list;
}

export function toggleAttacker(draft, cardId, defenderId) {
  const next = { ...draft };
  if (next[cardId] === defenderId) delete next[cardId];
  else next[cardId] = defenderId;
  return next;
}

export function attackBody(draft) {
  return { attackers: Object.entries(draft).map(([card_id, defender]) => ({ card_id, defender })) };
}

export function attacksAgainstYou(state) {
  return ((state.combat && state.combat.attacks) || []).filter((a) => a.defender === state.you || (state.battlefield || []).some((c) => c.id === a.defender && c.controller === state.you));
}

export function blockCandidates(state) {
  return (state.battlefield || []).filter((c) => c.controller === state.you && !c.tapped && /creature/i.test(c.type_line || ""));
}

export function toggleBlock(draft, blockerId, attackerId) {
  const next = { ...draft };
  if (next[blockerId] === attackerId) delete next[blockerId];
  else next[blockerId] = attackerId;
  return next;
}

export function blockBody(draft, you) {
  return { player: you, blocks: Object.entries(draft).map(([blocker, blocked]) => ({ blocker, blocked })) };
}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_decisions.py tests/test_ui_combat.py tests/test_card_split_labels.py tests/test_fetchland_search_choice.py tests/test_trigger_order_labels.py tests/test_ui_decision_coverage.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/decisions.js ui/combat.js tests/ && git commit -m "Decision and combat view models"
```

---

## Part 3: the screen

### Task 13: `dom.js`, `api.js`, `store.js`, `symbols.js`, `cards.js`

**Files:**
- Create: `ui/dom.js`, `ui/api.js`, `ui/store.js`, `ui/symbols.js`, `ui/cards.js`
- Test: `tests/test_ui_dom.py` (patching under a tiny fake DOM), and port `tests/test_ui_ai_stall.py` (move `aiStallNotice` and `AI_STALL_SECONDS` into `ui/store.js` unchanged and point the test at it).

**Interfaces:**
- `dom.js`: `h(tag, attrs, ...children) -> Element` (attrs: strings, `class`, `dataset` object, `on{Event}` handlers; children: nodes or strings); `patchChildren(parent, items, keyOf, create, update)` keeps existing children whose key survives (matched by `data-key`), creates missing ones, removes extras, and reorders in place; `setText(el, text)` only when changed.
- `api.js`: `api(method, path, body) -> Promise<json>` (throws `Error` with `.status` and `.detail` on a non-2xx, exactly as `ui/app.js:110-132`), `withUid(path, uid)`.
- `store.js`: `createStore(initial) -> {get(), set(patch), subscribe(fn)}`; the state shape `{gameId, uid, aiUid, you, state, log: {entries, lastSeq}, action, decisionDraft, combatDraft, run, stops, symbols, aiStatus, connection}`; `aiStallNotice`, `AI_STALL_SECONDS` moved from `ui/app.js` verbatim.
- `symbols.js`: `loadSymbols(api) -> Promise<map>`, `symbolNode(map, token, big) -> Node`, `withSymbols(map, text, big) -> DocumentFragment` (the old `withSymbols` at `ui/app.js:223` rebuilt over nodes instead of HTML strings), `manaPoolNodes(map, pool)`.
- `cards.js`: `cardNode(card, sizeClass, opts) -> Element` with the same classes the old `cardEl` set (`card`, size, `tapped`, `selected`, `attacking`, `blocking`, `summoning-sick`, `targetable`, `chosen`, `dimmed`, plus new `preview-tap`, `pinned`, `active-slot`), `data-key = card.id`; `updateCardNode(el, card, opts)` toggles those classes and the badges in place; `badges(card)` ported from `badgesHtml`; `cardBackNode()`.

- [ ] **Step 1: Write the failing dom test**

```python
from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

FAKE_DOM = """
class El { constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.textContent = ""; this.classList = { set: new Set(), add: (c) => this.classList.set.add(c), remove: (c) => this.classList.set.delete(c), toggle: (c, on) => on ? this.classList.set.add(c) : this.classList.set.delete(c), contains: (c) => this.classList.set.has(c) }; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); c.parentNode = this; return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; } addEventListener() {} get firstChild() { return this.children[0] || null; } get childNodes() { return this.children; }
}
globalThis.document = { createElement: (t) => new El(t), createTextNode: (s) => ({ nodeType: 3, textContent: s }), createDocumentFragment: () => new El("#fragment") };
"""


def test_patch_children_keeps_nodes_with_the_same_key() -> None:
    result = run_module(
        "dom.js",
        FAKE_DOM + """
        (() => {
          const parent = document.createElement("div");
          const create = (item) => { const el = m.h("div", { dataset: { key: item.id } }, item.id); el.made = item.id; return el; };
          const update = (el, item) => { el.updated = (el.updated || 0) + 1; };
          m.patchChildren(parent, [{ id: "a" }, { id: "b" }], (i) => i.id, create, update);
          const first = parent.children[0];
          m.patchChildren(parent, [{ id: "b" }, { id: "a" }, { id: "c" }], (i) => i.id, create, update);
          return [parent.children.map((c) => c.dataset.key), parent.children[1] === first, first.updated];
        })()""",
    )
    assert result == [["b", "a", "c"], True, 2]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ui_dom.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `ui/dom.js`**

```js
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") for (const [k, v] of Object.entries(value)) el.dataset[k] = v;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  return el;
}

export function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function patchChildren(parent, items, keyOf, create, update) {
  const existing = new Map();
  for (const child of [...parent.childNodes]) {
    if (child.dataset && child.dataset.key !== undefined) existing.set(child.dataset.key, child);
  }
  const wanted = new Set();
  let cursor = parent.firstChild;
  for (const item of items) {
    const key = String(keyOf(item));
    wanted.add(key);
    let el = existing.get(key);
    if (!el) {
      el = create(item);
      el.dataset.key = key;
    }
    update(el, item);
    if (el !== cursor) parent.insertBefore(el, cursor);
    else cursor = cursor.nextSibling === undefined ? parent.childNodes[parent.childNodes.indexOf(cursor) + 1] || null : cursor.nextSibling;
  }
  for (const [key, el] of existing) if (!wanted.has(key)) parent.removeChild(el);
}
```

The `cursor` advance handles both a real DOM (`nextSibling`) and the test's fake; keep both branches.

Implement `ui/api.js` by moving `api` and `withUid` from `ui/app.js:110-137`, with `withUid(path, uid)` taking the uid explicitly. Implement `ui/store.js`:

```js
export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      for (const fn of listeners) fn(state);
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
```

and append `aiStallNotice` and `AI_STALL_SECONDS` moved verbatim from `ui/app.js` (find them with `grep -n "function aiStallNotice\|AI_STALL_SECONDS =" ui/app.js`), exported. Port `tests/test_ui_ai_stall.py` to `run_module("store.js", "m.aiStallNotice(...)")`.

`ui/symbols.js`: port `loadSymbols`, `symbolHtml`, `withSymbols`, `manaPoolHtml` from `ui/app.js:196-260` so they build nodes with `h()` instead of HTML strings; `withSymbols(map, text, big)` splits on the same `/(\{[^}]{1,8}\})|([^{]+)/g` regex and returns a fragment. `ui/cards.js`: port `cardVisualInner`, `badgesHtml`, `cardEl`, `cardBackHtml` from `ui/app.js:560-655` to node builders; `updateCardNode(el, card, opts)` toggles each class with `classList.toggle` and replaces only the badges container's children.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_dom.py tests/test_ui_ai_stall.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/dom.js ui/api.js ui/store.js ui/symbols.js ui/cards.js tests/test_ui_dom.py tests/test_ui_ai_stall.py && git commit -m "Keyed DOM patching and the shared UI helpers"
```

### Task 14: `render/board.js` (board, hand, stack, red zone, mana preview)

**Files:**
- Create: `ui/render/board.js`
- Modify: `ui/index.html` (new shell), `ui/style.css` (new classes; keep the existing ones that still apply)

**Interfaces:**
- Consumes: `cards.js`, `dom.js`, `targeting.js` (`activeSlotIndex`, `candidateIds`), `combat.js`, store state.
- Produces: `mountBoard(root, store, handlers)` that subscribes to the store and patches `#opp-info`, `#opp-hand`, `#opp-battlefield`, `#stack-row`, `#red-zone`, `#my-battlefield`, `#my-hand`, `#you-info`. `handlers = {onHandCard(card), onPermanent(card, event), onPlayer(username), onStackObject(obj), onLand(card)}`. Per-card `opts` are computed from state: `targetable` when the active slot's `candidateIds` contains the id, `chosen` when any slot holds it, `dimmed` when an action is targeting and the card is not a candidate, `previewTap` when `state.preview.tapped` contains it, `pinned` when `action.pinned` does, `attacking` from `combatDraft` or `state.combat.attacks`, `blocking` likewise, and a small badge showing `N` when the same id sits in N slots.

- [ ] **Step 1: Write the new `index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>magic-cards-model: play</title>
<link rel="stylesheet" href="style.css" />
</head>
<body>
<section id="lobby">
  <!-- unchanged from the previous file: deck pickers, seed, Start game -->
</section>
<section id="game" hidden>
  <header id="topbar">
    <div id="turn-info"></div>
    <ol id="phase-track"></ol>
    <div class="topbar-right">
      <div id="ai-panel"></div>
      <div id="conn-status-game" class="pill"></div>
      <button id="open-zones" class="ghost">Zones</button>
      <button id="leave-game" class="ghost">Lobby</button>
    </div>
  </header>
  <div id="banners">
    <div id="game-over" hidden></div>
    <div id="pregame" class="notice" hidden></div>
    <div id="ai-stall-banner" hidden></div>
  </div>
  <div id="mtg-board">
    <div id="opp-info" class="info-row opp"></div>
    <div id="opp-hand" class="hand-row hand-row-opp"></div>
    <div id="opp-battlefield" class="battlefield-row"></div>
    <div id="stack-row" class="stack-row"></div>
    <div id="red-zone" class="red-zone" hidden></div>
    <div id="my-battlefield" class="battlefield-row"></div>
    <div id="casting-strip" class="casting-strip" hidden></div>
    <div id="my-hand" class="hand-row"></div>
    <div id="you-info" class="info-row you"></div>
  </div>
  <aside id="side-panel">
    <div id="prompt-box"></div>
    <div id="priority-box"></div>
    <div id="stops-box"></div>
    <div id="log-box"></div>
  </aside>
  <div id="card-preview" class="card-preview" hidden></div>
  <div id="toast" class="toast" hidden></div>
  <div id="popover" class="popover" hidden></div>
  <div id="modal" class="modal" hidden></div>
</section>
<script type="module" src="app.js"></script>
</body>
</html>
```

Copy the lobby section verbatim from the old file. Keep `tests/test_ui_routes.py` green: it asserts `src="app.js"` in `/` and that `/app.js` contains `startGame`; keep a `startGame` function name in the new `app.js`. Update `tests/test_ui_hidden_elements.py` to scan every `ui/*.js` and `ui/render/*.js` for `.hidden =` toggles (same regex, glob over the modules).

- [ ] **Step 2: Implement `ui/render/board.js`**

```js
import { cardNode, updateCardNode, cardBackNode } from "../cards.js";
import { h, patchChildren, clear, setText } from "../dom.js";
import { activeSlotIndex, candidateIds } from "../targeting.js";
import { manaPoolNodes } from "../symbols.js";

function targetingOpts(app, id) {
  const action = app.action;
  const decision = app.decisionDraft && app.state.pending_decision && app.state.pending_decision.player === app.state.you ? app.decisionTargets : null;
  const slots = decision ? decision.slots : action ? action.slots : [];
  const aff = decision ? decision.affordance : action ? action.affordance : null;
  const picking = Boolean(aff && aff.targets_anything && slots.length);
  const active = picking ? activeSlotIndex(slots) : -1;
  const legal = picking && active >= 0 ? candidateIds(aff, slots[active]) : new Set();
  const held = slots.filter((s) => s.targets.includes(id)).length;
  return { picking, targetable: legal.has(id), chosen: held > 0, heldCount: held, dimmed: picking && !legal.has(id) && held === 0 };
}

function permanentOpts(app, card) {
  const opts = targetingOpts(app, card.id);
  const attacks = (app.state.combat && app.state.combat.attacks) || [];
  opts.attacking = Boolean((app.combatDraft && app.combatDraft[card.id]) || attacks.some((a) => a.attacker === card.id));
  opts.blocking = Boolean((app.combatDraft && Object.keys(app.combatDraft).includes(card.id) && app.state.step === "declare_blockers") || attacks.some((a) => (a.blockers || []).includes(card.id)));
  opts.previewTap = Boolean(app.preview && app.preview.tapped && app.preview.tapped.some((t) => t.card_id === card.id));
  opts.pinned = Boolean(app.action && app.action.pinned.some((p) => p.card_id === card.id));
  opts.lifted = Boolean(app.action && app.action.cardId === card.id);
  return opts;
}

function patchCards(container, cards, app, size, onClick) {
  patchChildren(
    container, cards, (c) => c.id,
    (card) => cardNode(card, size, {}),
    (el, card) => {
      updateCardNode(el, card, permanentOpts(app, card));
      el.onclick = (event) => onClick(card, event);
    }
  );
}

function infoRow(el, app, player, isYou) {
  const opts = targetingOpts(app, player.username);
  el.classList.toggle("targetable", opts.targetable);
  el.classList.toggle("chosen", opts.chosen);
  el.classList.toggle("dimmed", opts.dimmed);
  el.classList.toggle("active", app.state.active_player === player.username);
  el.classList.toggle("priority", app.state.priority_holder === player.username);
  clear(el);
  el.appendChild(h("span", { class: "name" }, player.username));
  el.appendChild(h("span", { class: "life", title: "life" }, String(player.life)));
  el.appendChild(h("span", { class: "counts" }, `hand ${player.hand_count} / library ${player.library_count} / graveyard ${player.graveyard_count}`));
  el.appendChild(h("span", { class: "pool" }, manaPoolNodes(app.symbols, player.mana_pool)));
  if (opts.heldCount > 1) el.appendChild(h("span", { class: "held-count" }, `x${opts.heldCount}`));
}

export function mountBoard(root, store, handlers) {
  const $ = (sel) => root.querySelector(sel);
  const els = {
    oppInfo: $("#opp-info"), oppHand: $("#opp-hand"), oppBf: $("#opp-battlefield"), stack: $("#stack-row"),
    redZone: $("#red-zone"), myBf: $("#my-battlefield"), myHand: $("#my-hand"), youInfo: $("#you-info"),
  };
  els.oppInfo.onclick = () => handlers.onPlayer(store.get().opponent);
  els.youInfo.onclick = () => handlers.onPlayer(store.get().state.you);
  return store.subscribe((app) => {
    const state = app.state;
    if (!state) return;
    const you = state.you;
    const me = state.players.find((p) => p.username === you);
    const opp = state.players.find((p) => p.username !== you);
    infoRow(els.oppInfo, app, opp, false);
    infoRow(els.youInfo, app, me, true);
    patchChildren(els.oppHand, Array.from({ length: opp.hand_count }, (_, i) => ({ id: `back-${i}` })), (c) => c.id, () => cardBackNode(), () => {});
    patchCards(els.oppBf, state.battlefield.filter((c) => c.controller !== you), app, "card-md", handlers.onPermanent);
    patchCards(els.myBf, state.battlefield.filter((c) => c.controller === you), app, "card-md", handlers.onPermanent);
    patchCards(els.myHand, state.hands[you] || [], app, "card-lg", handlers.onHandCard);
    patchChildren(els.stack, state.stack, (o) => o.id,
      (obj) => h("div", { class: "stack-object" }, h("span", { class: "name" }), h("span", { class: "targets" })),
      (el, obj) => {
        const opts = targetingOpts(app, obj.id);
        el.classList.toggle("targetable", opts.targetable);
        el.classList.toggle("chosen", opts.chosen);
        setText(el.querySelector(".name"), `${obj.name}${obj.kind === "ability" ? " (ability)" : ""}`);
        setText(el.querySelector(".targets"), (obj.targets || []).length ? `-> ${obj.targets.join(", ")}` : "");
        el.onclick = () => handlers.onStackObject(obj);
      });
    const attacking = state.battlefield.filter((c) => (app.combatDraft && app.combatDraft[c.id]) || (state.combat.attacks || []).some((a) => a.attacker === c.id));
    els.redZone.hidden = attacking.length === 0;
    patchCards(els.redZone, attacking, app, "card-sm", handlers.onPermanent);
  });
}
```

Add to `style.css`: `.red-zone` (a thin row with a red border), `.casting-strip` (a row above the hand), `.card.preview-tap` (a translucent tap rotation), `.card.pinned` (a gold outline), `.card.lifted` (raised, shadow), `.held-count`, `.stack-object.targetable`, `.toast`, `.popover` (absolute, anchored by `style.left/top` set by the opener), `#side-panel` sections `#prompt-box`, `#priority-box`, `#stops-box`, `#log-box` stacked with `#log-box` taking the remaining height with `overflow-y: auto`.

- [ ] **Step 3: Verify it loads**

There is no automated check for the DOM module yet; the e2e task covers it. Confirm `node --input-type=module -e 'import("./ui/render/board.js")'` exits 0 when run from the repo root (it will fail on `document` only when a function runs, not on import).

- [ ] **Step 4: Commit**

```bash
git add ui/index.html ui/style.css ui/render/board.js tests/test_ui_hidden_elements.py && git commit -m "Board rendering with keyed patching, red zone and mana preview"
```

### Task 15: `render/ribbon.js` (prompt, casting strip, decision, priority, stops, popover)

**Files:**
- Create: `ui/render/ribbon.js`

**Interfaces:**
- Consumes: `actions.js`, `decisions.js`, `priority.js`, `targeting.js`, `mana.js`, `symbols.js`, `dom.js`.
- Produces: `mountRibbon(root, store, handlers)` with `handlers = {onToggleMode(n), onSetX(n), onChooseColor(c), onChooseAltCost(i), onTogglePitch(id), onToggleConvoke(id), onToggleDelve(id), onFocusSlot(i), onUnfill(slotIndex, id), onSetShare(id, n), onDone(), onCancel(), onHoldPriority(bool), onDecisionDraft(patch), onDecide(answer), onVerb(verb), onStopRun(), onToggleStop(side, step), onAttackConfirm(), onAttackNone(), onBlockConfirm(), onBlockNone(), onConcede()}`; and `showPopover(anchorEl, items)` / `hidePopover()` where `items = [{label, sublabel, disabled, title, onSelect}]` for the inline ability menu.

- [ ] **Step 1: Implement**

Sections rendered into `#prompt-box` in priority order: game over; pregame (port `renderMulliganStep`/`renderOpeningHandStep` from `ui/app.js:1182-1275` to node builders, same requests); decision (from `viewModel(pending, app.decisionDraft)`: title, prompt, `remaining` seconds from `state.decision_timeouts` when present, controls by type: `options` as toggle buttons, `number` as an input, `targets` as slot chips (board clicks handled by app.js), `split` as two columns with Move/Keep buttons and up/down arrows for the kept order, `order` as click-in-order list; submit and secondary buttons); the casting strip content (when `app.action`): card name, mode chips with `selected`, an X input, colour chips from `colorOptions`, alternative-cost buttons (disabled with `title = unavailable` reason as before), convoke/delve/pitch pickers listing the affordance's `object_costs`/`requires` candidates, slot chips (`label`, filled target names, active highlighted, click to focus, x to unfill), share steppers when a division exists, a "Done" button when an "up to" slot is open, "Keep priority" checkbox, Cancel, and `action.error` in red; the combat prompt (declare attackers: "Attack" enabled when the draft is non-empty, "No attack"; declare blockers: list of drawn blocks, "Block", "No blocks"); otherwise the hint "click a card to play it".

`#priority-box`: one button per `passButtonsFor(state)` verb with `VERB_LABELS`, or "Stop (running: <reason>)" when `app.run`; a "Concede" ghost button.

`#stops-box`: a two-column checkbox grid over `STEPS` minus `untap`/`cleanup`, columns "my turn" and "their turn", checked from `app.stops`.

Popover: `showPopover(anchor, items)` positions `#popover` under the anchor's bounding rect and lists items as buttons; clicking outside or Escape hides it.

Where the old code built HTML strings, build nodes with `h()`; use `withSymbols(app.symbols, text)` for any text with mana symbols.

- [ ] **Step 2: Commit**

```bash
git add ui/render/ribbon.js && git commit -m "Ribbon: prompt, casting strip, decisions, verbs, stops"
```

### Task 16: `render/log.js` and `render/lobby.js`

**Files:**
- Create: `ui/render/log.js`, `ui/render/lobby.js`

**Interfaces:**
- `mountLog(root, store)`: renders `app.log.entries` newest last into `#log-box`, one line per entry: `#seq [turn/step] player action` and, when `entry.detail.allowed === false`, the `reason` in red (class `refusal`); `report_problem` entries in bold; auto-scrolls to the bottom when the user was already at the bottom.
- `mountLobby(root, store, handlers)`: port the lobby wiring from `ui/app.js` (`loadDecks`, validate, upload, `startGame`) to the module, keeping the function name `startGame` exported from `app.js` for `tests/test_ui_routes.py`.

- [ ] **Step 1: Implement both** by porting the lobby code from `ui/app.js:240-400` (find with `grep -n "function startGame\|function loadDecks\|function validateDeck" ui/app.js`) and writing the log renderer with `patchChildren` keyed by `seq`.

- [ ] **Step 2: Commit**

```bash
git add ui/render/log.js ui/render/lobby.js && git commit -m "Game log panel and lobby module"
```

### Task 17: `app.js` (boot, polling, click flows, firing)

**Files:**
- Create: `ui/app.js` (replace the old file entirely; `git rm` nothing, overwrite)

**Interfaces:**
- Consumes: everything above.
- Produces: the running application. Exports `startGame` for the routes test.

- [ ] **Step 1: Implement the flows**

```js
import { api, withUid } from "./api.js";
import { createStore, aiStallNotice, AI_STALL_SECONDS } from "./store.js";
import { loadSymbols } from "./symbols.js";
import * as A from "./actions.js";
import * as P from "./priority.js";
import * as C from "./combat.js";
import { untappedSources, colorOptions } from "./mana.js";
import { viewModel, answerFor } from "./decisions.js";
import { mountBoard } from "./render/board.js";
import { mountRibbon, showPopover, hidePopover } from "./render/ribbon.js";
import { mountLog } from "./render/log.js";
import { mountLobby } from "./render/lobby.js";

const POLL_MS = 1200;
const store = createStore({ gameId: null, uid: null, aiUid: null, state: null, action: null, decisionDraft: {}, combatDraft: {}, run: null,
                            stops: P.loadStops(localStorage), symbols: {}, log: { entries: [], lastSeq: 0 }, preview: null, aiStatus: null, connection: "idle" });

function you() { return store.get().state.you; }
function gamePath(rest) { return `/api/games/${store.get().gameId}/${rest}`; }

async function poll() {
  const app = store.get();
  if (!app.gameId || app.polling) return;
  store.set({ polling: true });
  try {
    const state = await api("GET", withUid(gamePath(""), app.uid));
    const log = await api("GET", withUid(gamePath(`log?since=${app.log.lastSeq}`), app.uid));
    store.set((s) => ({ state, connection: "ok", log: { entries: [...s.log.entries, ...log.log].slice(-500), lastSeq: log.last_seq } }));
    if (app.aiUid) store.set({ aiStatus: await api("GET", `/api/ui/games/${app.gameId}/ai-seat/status?uid=${app.aiUid}`) });
    await tickRun();
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
  } finally {
    store.set({ polling: false });
  }
}

// ---- actions -------------------------------------------------------------

async function refreshAffordance() {
  const app = store.get();
  const action = app.action;
  if (!action || action.kind === "special") return;
  const query = new URLSearchParams({ player: you() });
  const q = A.affordanceQuery(action);
  if (q.ability_index !== undefined) query.set("ability_index", q.ability_index);
  if (q.modes) query.set("modes", q.modes.join(","));
  if (q.x_value !== undefined) query.set("x_value", q.x_value);
  if (action.kind === "cast" && !A.modesReady(action)) return;
  const affordance = await api("GET", gamePath(`affordances/${action.cardId}?${query}`));
  store.set((s) => (s.action && s.action.cardId === action.cardId ? { action: A.withAffordance(s.action, affordance) } : {}));
  await previewAndMaybeFire();
}

async function previewAndMaybeFire() {
  const app = store.get();
  const action = app.action;
  if (!action) return;
  if (!A.readyToFire(action)) { store.set({ preview: null }); return; }
  const body = { ...A.bodyFor(action, you(), untappedSources(app.state)), dry_run: true };
  const probe = await api("POST", gamePath(A.endpointFor(action)), body);
  if (probe.allowed === false) {
    store.set({ action: { ...action, error: probe.reason }, preview: { tapped: probe.tapped || [] } });
    return;
  }
  store.set({ preview: { tapped: probe.tapped || [] } });
  if (action.kind !== "special" && needsExplicitFire(action)) return;
  await fire();
}

// An "up to"/unbounded slot or a division waits for the player's Done, and
// pinning mana waits too: the preview is shown, the player confirms by
// clicking Cast on the strip.
function needsExplicitFire(action) {
  return action.pinned.length > 0 || (action.affordance && action.affordance.division) || action.slots.some((s) => s.count === null || s.minimum < (s.count || 0));
}

async function fire() {
  const app = store.get();
  const action = app.action;
  const body = A.bodyFor(action, you(), untappedSources(app.state));
  const res = await api("POST", gamePath(A.endpointFor(action)), body);
  if (res.allowed === false) { store.set({ action: { ...action, error: res.reason } }); toast(res.reason); return; }
  store.set({ action: null, preview: null });
  const onStack = !(res.mana_ability || action.kind === "special");
  if (onStack && !action.holdPriority) await passOnce();
  await poll();
}

async function onHandCard(card) {
  const app = store.get();
  if (app.action && app.action.cardId === card.id) return cancel();
  if (app.action) return tryTarget(card.id);
  const specials = Object.keys(card.special_actions || {}).filter((k) => k !== "cycling");
  const cycling = (card.special_actions && card.special_actions.cycling) || [];
  if (/land/i.test(card.type_line || "") && !specials.length) return playLand(card);
  if (specials.length || cycling.length) {
    return showPopover(document.querySelector(`[data-key="${card.id}"]`), [
      { label: "Cast", onSelect: () => beginCast(card) },
      ...specials.map((k) => ({ label: k[0].toUpperCase() + k.slice(1), sublabel: card.special_actions[k].cost, onSelect: () => fireSpecial(card, k) })),
      ...cycling.map((c) => ({ label: "Cycle", sublabel: c.cost, onSelect: () => cycle(card) })),
    ]);
  }
  return beginCast(card);
}

async function beginCast(card) {
  hidePopover();
  store.set({ action: A.begin("cast", card, {}), preview: null });
  if (!A.needsChoices(card, null)) return previewAndMaybeFire();
  return refreshAffordance();
}

async function playLand(card) {
  const res = await api("POST", gamePath("actions/play-land"), { player: you(), card_id: card.id });
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function fireSpecial(card, keyword) {
  hidePopover();
  const res = await api("POST", gamePath(`actions/${keyword}`), { player: you(), card_id: card.id });
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function cycle(card) {
  hidePopover();
  const res = await api("POST", gamePath("actions/cycle"), { player: you(), card_id: card.id });
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function onPermanent(card, event) {
  const app = store.get();
  const state = app.state;
  if (app.action || (state.pending_decision && state.pending_decision.player === you() && state.pending_decision.kind === "choose_targets")) {
    if (app.action && card.controller === you() && (card.abilities || []).some((a) => a.is_mana_ability) && !isCandidate(card.id)) return pinLand(card, event);
    return tryTarget(card.id);
  }
  if (state.step === "declare_attackers" && state.active_player === you() && state.priority_holder === you() && card.controller === you()) {
    return toggleAttacker(card, event);
  }
  if (state.step === "declare_blockers" && state.active_player !== you() && state.priority_holder === you()) return toggleBlock(card);
  if (card.controller !== you()) return;
  const abilities = card.abilities || [];
  const vehicle = /vehicle/i.test(card.type_line || "");
  if (!abilities.length && !vehicle) return toast(`${card.name} has no activated abilities`);
  if (abilities.length === 1 && !vehicle) return beginActivate(card, 0);
  const items = abilities.map((a) => ({ label: a.text, sublabel: a.cost || "", onSelect: () => beginActivate(card, a.index) }));
  if (vehicle) items.push({ label: "Crew", onSelect: () => beginCrew(card) });
  showPopover(event.currentTarget, items);
}

async function beginActivate(card, index) {
  hidePopover();
  let action = A.begin("activate", card, { abilityIndex: index });
  const colors = colorOptions(card, index);
  if (colors.length === 1) action = A.chooseColor(action, colors[0]);
  store.set({ action, preview: null });
  if (!A.needsChoices(card, index) || (colors.length <= 1 && !(card.abilities[index].targeting || {}).targets_anything && !card.abilities[index].needs_x)) return previewAndMaybeFire();
  return refreshAffordance();
}

function isCandidate(id) {
  const action = store.get().action;
  return Boolean(action && action.affordance && action.affordance.candidates.some((c) => c.id === id));
}

async function pinLand(card, event) {
  const app = store.get();
  const colors = colorOptions(card, 0);
  const pinnedAlready = app.action.pinned.some((p) => p.card_id === card.id);
  if (pinnedAlready) { store.set({ action: A.unpinSource(app.action, card.id) }); return previewAndMaybeFire(); }
  if (colors.length <= 1) { store.set({ action: A.pinSource(app.action, card.id, colors[0] || "C") }); return previewAndMaybeFire(); }
  showPopover(event.currentTarget, colors.map((c) => ({ label: `Tap for {${c}}`, onSelect: () => { hidePopover(); store.set((s) => ({ action: A.pinSource(s.action, card.id, c) })); previewAndMaybeFire(); } })));
}

async function tryTarget(id) {
  const app = store.get();
  const pending = app.state.pending_decision;
  if (pending && pending.player === you() && pending.kind === "choose_targets" && !app.action) {
    const draft = app.decisionDraft.targets || [];
    const next = draft.includes(id) ? draft.filter((t) => t !== id) : [...draft, id];
    return store.set({ decisionDraft: { ...app.decisionDraft, targets: next } });
  }
  if (!app.action) return;
  if (app.action.affordance && app.action.affordance.understood === false) return store.set({ action: A.addFreeTarget(app.action, id) });
  const next = A.chooseTarget(app.action, id);
  if (next === app.action) return;
  store.set({ action: next });
  await previewAndMaybeFire();
}

function cancel() { hidePopover(); store.set({ action: null, preview: null }); }

// ---- combat --------------------------------------------------------------

function toggleAttacker(card, event) {
  const app = store.get();
  const defenders = C.defenders(app.state);
  if (!C.attackCandidates(app.state).some((c) => c.id === card.id) && !app.combatDraft[card.id]) return toast(`${card.name} cannot attack now`);
  if (defenders.length <= 1 || app.combatDraft[card.id]) return store.set({ combatDraft: C.toggleAttacker(app.combatDraft, card.id, app.combatDraft[card.id] || defenders[0].id) });
  showPopover(event.currentTarget, defenders.map((d) => ({ label: `Attack ${d.label}`, onSelect: () => { hidePopover(); store.set((s) => ({ combatDraft: C.toggleAttacker(s.combatDraft, card.id, d.id) })); } })));
}

async function confirmAttack() {
  const body = C.attackBody(store.get().combatDraft);
  const res = await api("POST", gamePath("actions/attack"), body);
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await poll();
}

let pendingBlocker = null;
function toggleBlock(card) {
  const app = store.get();
  if (card.controller === you()) { pendingBlocker = card.id; return toast(`${card.name}: now click the attacker it blocks`); }
  if (!pendingBlocker) return;
  store.set({ combatDraft: C.toggleBlock(app.combatDraft, pendingBlocker, card.id) });
  pendingBlocker = null;
}

async function confirmBlocks() {
  const res = await api("POST", gamePath("actions/declare-blockers"), C.blockBody(store.get().combatDraft, you()));
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await poll();
}

// ---- priority ------------------------------------------------------------

async function passOnce() {
  const res = await api("POST", gamePath("actions/pass-priority"), { player: you() });
  const out = P.interpretPassOutcome(res);
  if (!out.ok) toast(out.reason);
  return out;
}

async function onVerb(verb) {
  const app = store.get();
  if (verb === "pass") { await passOnce(); return poll(); }
  store.set({ run: P.startRun(verb, app.state) });
  await tickRun();
}

async function castable() {
  const app = store.get();
  const hand = app.state.hands[you()] || [];
  for (const card of hand) {
    if (/land/i.test(card.type_line || "")) continue;
    const res = await api("POST", gamePath("actions/cast"), { player: you(), card_id: card.id, mana_sources: untappedSources(app.state), dry_run: true });
    if (res.allowed) return true;
  }
  return false;
}

async function tickRun() {
  const app = store.get();
  if (!app.run || !app.state) return;
  if (app.state.priority_holder !== you()) return;
  const reason = P.stopReason(app.state, app.run, app.stops, app.run.verb === "until_castable" ? await castable() : false);
  if (reason) { store.set({ run: null }); toast(`stopped: ${reason}`); return; }
  const out = await passOnce();
  store.set((s) => ({ run: s.run ? { ...s.run, passes: s.run.passes + 1, knownStackIds: s.state.stack.map((o) => o.id) } : null }));
  if (!out.ok) store.set({ run: null });
}

// ---- decisions -----------------------------------------------------------

async function decide(answer) {
  const res = await api("POST", withUid(gamePath("decide"), store.get().uid), { player: you(), ...answer });
  if (res.allowed === false) toast(res.reason);
  store.set({ decisionDraft: {} });
  await poll();
}

function toast(text) {
  const el = document.querySelector("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
}

// ---- boot ----------------------------------------------------------------

export async function startGame(created) {
  store.set({ gameId: created.game_id, uid: created.uid, aiUid: created.aiUid, log: { entries: [], lastSeq: 0 } });
  localStorage.setItem("mcm.game", JSON.stringify({ gameId: created.game_id, uid: created.uid, aiUid: created.aiUid }));
  document.querySelector("#lobby").hidden = true;
  document.querySelector("#game").hidden = false;
  await poll();
}

async function main() {
  store.set({ symbols: await loadSymbols(api) });
  mountLobby(document, store, { startGame });
  mountBoard(document, store, { onHandCard, onPermanent, onPlayer: (u) => tryTarget(u), onStackObject: (o) => tryTarget(o.id) });
  mountRibbon(document, store, {
    onToggleMode: (n) => { store.set((s) => ({ action: A.toggleMode(s.action, n) })); refreshAffordance(); },
    onSetX: (n) => { store.set((s) => ({ action: A.setX(s.action, n) })); refreshAffordance(); },
    onChooseColor: (c) => { store.set((s) => ({ action: A.chooseColor(s.action, c) })); previewAndMaybeFire(); },
    onChooseAltCost: (i) => { store.set((s) => ({ action: A.chooseAltCost(s.action, i) })); previewAndMaybeFire(); },
    onTogglePitch: (id) => { store.set((s) => ({ action: A.togglePitch(s.action, id) })); previewAndMaybeFire(); },
    onToggleConvoke: (id) => { store.set((s) => ({ action: A.toggleConvoke(s.action, id) })); previewAndMaybeFire(); },
    onToggleDelve: (id) => { store.set((s) => ({ action: A.toggleDelve(s.action, id) })); previewAndMaybeFire(); },
    onUnfill: (slotIndex, id) => { store.set((s) => ({ action: { ...s.action, slots: s.action.slots.map((sl, i) => (i === slotIndex ? { ...sl, targets: sl.targets.filter((t) => t !== id) } : sl)) } })); previewAndMaybeFire(); },
    onSetShare: (id, n) => { store.set((s) => ({ action: A.setShare(s.action, id, n) })); },
    onDone: () => { store.set((s) => ({ action: A.markDone(s.action) })); fire(); },
    onCast: () => fire(),
    onCancel: cancel,
    onHoldPriority: (hold) => store.set((s) => ({ action: { ...s.action, holdPriority: hold } })),
    onDecisionDraft: (patch) => store.set((s) => ({ decisionDraft: { ...s.decisionDraft, ...patch } })),
    onDecide: decide,
    onVerb, onStopRun: () => store.set({ run: null }),
    onToggleStop: (side, step) => store.set((s) => { const list = s.stops[side].includes(step) ? s.stops[side].filter((x) => x !== step) : [...s.stops[side], step]; const stops = { ...s.stops, [side]: list }; P.saveStops(localStorage, stops); return { stops }; }),
    onAttackConfirm: confirmAttack, onAttackNone: () => passOnce().then(poll),
    onBlockConfirm: confirmBlocks, onBlockNone: () => passOnce().then(poll),
    onConcede: async () => { await api("POST", gamePath("actions/concede"), { player: you() }); await poll(); },
  });
  mountLog(document, store);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { cancel(); hidePopover(); } });
  const saved = JSON.parse(localStorage.getItem("mcm.game") || "null");
  if (saved && saved.gameId) await startGame({ game_id: saved.gameId, uid: saved.uid, aiUid: saved.aiUid });
  setInterval(poll, POLL_MS);
}

if (typeof window !== "undefined" && typeof document !== "undefined") main();
```

Port the remaining pieces from the old `ui/app.js` that this skeleton references but does not define: the AI panel (`renderAiPanel`, `showFallbackBox`, `showAiError`, the stall banner using `aiStallNotice`), the phase track and turn info, the zones modal (rebuilt as a popover listing graveyards and exile with `cardNode`s wired to `onPermanent` for targeting), `openReportModal`, and the pregame panel handlers. Keep `beginCrew(card)`: it begins an action of kind `"crew"` posting to `actions/crew` with `crewing` ids chosen by clicking creatures; add `crew` handling to `endpointFor`/`bodyFor` in `actions.js` (`{player, card_id, crewing}`) with a test in `tests/test_ui_actions.py`.

- [ ] **Step 2: Lint the modules**

Run: `for f in ui/*.js ui/render/*.js; do node --check "$f" || echo "FAIL $f"; done`
Expected: no `FAIL` lines.

- [ ] **Step 3: Run the whole UI test set**

Run: `uv run pytest tests/test_ui_*.py tests/test_card_split_labels.py tests/test_fetchland_search_choice.py tests/test_trigger_order_labels.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add ui/app.js && git commit -m "The play screen: one click when nothing is left to choose"
```

### Task 18: end-to-end Kozilek's Command through the HTTP API the UI uses

**Files:**
- Test: `tests/test_kozilek_command_ui_flow.py`

**Interfaces:**
- Consumes: the API as `app.js` calls it: `GET affordances` with `modes=1,2&x_value=1`, then `POST actions/cast` with `targets: ["alice", "alice"]`, `modes: [1, 2]`, `x_value: 1`, `mana_sources: [...]`, first as `dry_run` then for real, then two passes.

- [ ] **Step 1: Write the test**

```python
"""The exact calls `ui/app.js` makes for Kozilek's Command choosing modes 1
and 2 with the caster as both targets (CR 115.3: one object per instance of
the word "target"; two modes are two instances)."""

from __future__ import annotations

from conftest import add_card
from fastapi.testclient import TestClient


def test_same_player_for_both_modes_casts_and_resolves(client: TestClient) -> None:
    game_id = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    command = add_card(client, game_id, "alice", "Kozilek's Command", zone="hand")
    lands = [add_card(client, game_id, "alice", "Wastes", zone="battlefield") for _ in range(3)]
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main", "active_player": "alice"})
    aff = client.get(
        f"/api/games/{game_id}/affordances/{command}",
        params={"player": "alice", "modes": "1,2", "x_value": 1},
    ).json()
    assert [r["mode"] for r in aff["requirements"]] == [1, 2]
    body = {"player": "alice", "card_id": command, "targets": ["alice", "alice"], "modes": [1, 2], "x_value": 1, "mana_sources": lands}
    probe = client.post(f"/api/games/{game_id}/actions/cast", json={**body, "dry_run": True}).json()
    assert probe["allowed"] is True, probe
    assert len(probe["tapped"]) == 3
    res = client.post(f"/api/games/{game_id}/actions/cast", json=body).json()
    assert res["allowed"] is True, res
    (spell,) = client.get(f"/api/games/{game_id}").json()["stack"]
    assert spell["target_refs"] == [{"kind": "player", "id": "alice", "name": "alice"}] * 2
    for player in ("alice", "bob"):
        client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": player})
    board = client.get(f"/api/games/{game_id}").json()
    spawn = [c for c in board["battlefield"] if "Eldrazi Spawn" in (c["name"] or "")]
    assert len(spawn) == 1
    alice = next(p for p in board["players"] if p["username"] == "alice")
    assert alice["hand_count"] == 1  # drew from mode 2 (library was empty apart from nothing; adjust if the harness seeds a library)
```

If the harness boots players with empty libraries, mode 2's draw makes alice lose at the next state-based check; add one `add_card(..., zone="library")` for alice before casting and assert `hand_count == 1`. If the scry decision blocks resolution (a `scry` `pending_decision` for alice), answer it with `POST /decide` `{"player": "alice", "moved": [], "top_order": null}` using alice's uid from `POST /api/games`' `seat_uids` before the second pass, the way `tests/test_ui_e2e.py` answers decisions.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_kozilek_command_ui_flow.py -v`
Expected: PASS. A failure inside the engine's resolution of Kozilek's Command is an engine bug: report it in the task's result rather than reshaping the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_kozilek_command_ui_flow.py && git commit -m "Pin the Kozilek's Command two-slots-one-player flow end to end"
```

### Task 19: browser pass and docs

**Files:**
- Modify: `README.md` (the "Play it" section: one-click flows, slots, pins, verbs, stops, log), `docs/superpowers/specs/2026-09-04-play-ui-action-protocol-design.md` (note: `tapped` carries ids and names, no colour).

- [ ] **Step 1: Run the server**

Run: `mise run` in the api repo; open `http://127.0.0.1:8000/`.

- [ ] **Step 2: Walk each flow with a real deck** (the bundled `eldrazi_ramp.txt` against `boros_energy.txt`, AI seat in subprocess mode or the fallback prompt driven by hand with `seat_cli`): play a land (one click); cast a creature with nothing to choose (one click, lands preview then tap); cast Kozilek's Command choosing modes 1 and 2 with yourself as both targets; activate a single-ability permanent; open the popover on a multi-ability permanent; pin a dual land for a colour before a cast; declare two attackers with the red zone and confirm; block by clicking your creature then the attacker; answer a scry; use Pass turn and see it stop at a checked stop; toggle a stop and reload the page to confirm it persisted; read the log after a refused action. Use the Chrome tools (`mcp__claude-in-chrome__*`) to drive and screenshot each step, and save the screenshots to the scratchpad.

- [ ] **Step 3: Fix what the walk finds**, each fix as its own commit with a Node or pytest test where the pure logic was wrong.

- [ ] **Step 4: Full suite, lint**

Run: `mise run test && mise run lint`
Expected: PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Document the play screen's one-click flows, slots and stops"
```

## Self-review notes

- Spec coverage: interaction model (Tasks 17, 15, 14), targeting (8, 11, 1, 18), mana (9, 2, 17), priority (10, 17), protocol additions (1-7), code structure (13-17), testing (every task, 18, 19), decisions and log (12, 16, 6, 7).
- `shares` and `division`: Task 4 and Task 11 (`setShare`, `sharesValid`), rendered in Task 15.
- The old `PUT mana-pool` and `resolve-top` UI paths are gone on purpose; `advance` stays reachable only through the AI-stall banner's "move the game" button, ported in Task 17.
