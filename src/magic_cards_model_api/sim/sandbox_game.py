"""A sandbox game built and driven through this API's own HTTP routes.

Runs inside the sim worker over `TestClient(app)`. The admin key and the seat uids
never leave this object: every answer is redacted, and cards are addressed by labels
("@bolt") the spec gave them instead of ids.
"""

from __future__ import annotations

import copy
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from magic_cards_model_api.schemas import GameStateView
from magic_cards_model_api.sim.redact import redact, support_block, support_unknown

Credential = Literal["seat", "admin"]
ActionRoute = tuple[str, str, Credential]

_G = "/api/games/{game_id}"
_PATH_PARAM = re.compile(r"[A-Za-z0-9_.:-]+")


def _safe_path_value(value: str) -> bool:
    return bool(_PATH_PARAM.fullmatch(value)) and value not in (".", "..")


def _check_name(value: str, field: str) -> str:
    if not _safe_path_value(value):
        raise ValueError(f"invalid {field} {value!r}: use letters, digits and _ . : - only")
    return value


ACTIONS: dict[str, ActionRoute] = {
    # seat actions: the acting player's own uid
    "cast": ("POST", f"{_G}/actions/cast", "seat"),
    "activate": ("POST", f"{_G}/actions/activate", "seat"),
    "play_land": ("POST", f"{_G}/actions/play-land", "seat"),
    "attack": ("POST", f"{_G}/actions/attack", "seat"),
    "declare_blockers": ("POST", f"{_G}/actions/declare-blockers", "seat"),
    "pass": ("POST", f"{_G}/actions/pass-priority", "seat"),
    "concede": ("POST", f"{_G}/actions/concede", "seat"),
    "cycle": ("POST", f"{_G}/actions/cycle", "seat"),
    "crew": ("POST", f"{_G}/actions/crew", "seat"),
    "equip": ("POST", f"{_G}/actions/equip", "seat"),
    "target": ("POST", f"{_G}/actions/target", "seat"),
    "decide": ("POST", f"{_G}/decide", "seat"),
    "mulligan": ("POST", f"{_G}/pregame/mulligan", "seat"),
    "opening_hand_action": ("POST", f"{_G}/pregame/opening-hand/action", "seat"),
    "opening_hand_finish": ("POST", f"{_G}/pregame/opening-hand/finish", "seat"),
    # the route itself demands the admin key (it settles every seat at once)
    "settle": ("POST", f"{_G}/pregame/settle", "admin"),
    # admin engine actions
    "resolve_top": ("POST", f"{_G}/resolve-top", "admin"),
    "advance": ("POST", f"{_G}/advance", "admin"),
    "state_based_actions": ("POST", f"{_G}/state-based-actions", "admin"),
    # admin setup actions
    "add_card": ("POST", f"{_G}/cards", "admin"),
    "move_card": ("POST", f"{_G}/cards/{{card_id}}/move", "admin"),
    "tap_card": ("POST", f"{_G}/cards/{{card_id}}/tap", "admin"),
    "patch_card": ("PATCH", f"{_G}/cards/{{card_id}}", "admin"),
    "patch_player": ("PATCH", f"{_G}/players/{{username}}", "admin"),
    "mana_pool": ("PUT", f"{_G}/players/{{username}}/mana-pool", "admin"),
    "set_step": ("PUT", f"{_G}/step", "admin"),
}

PLAY_REFUSAL = "setup and engine actions are disabled in play mode"
SNAPSHOT_PLAY_REFUSAL = "snapshots are for board mode (sim_new); play mode cannot rewind"
DEFAULT_MAX_SNAPSHOTS = 16
OPPONENT_STILL_PLAYING = "opponent still playing; call sim_state to continue"
NO_PROGRESS = "no progress: neither seat can move; consider concede"
_MAX_OPPONENT_MOVES = 40  # headlines kept per answer; a whole baseline turn is well below it

_VIEW_FIELDS = frozenset(GameStateView.model_fields)
_ZONES = ("hands", "graveyards")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


MAX_LIBRARY_FILLER = 60


class LibraryFiller(_Strict):
    """`count` copies of one card put under the library, so draw steps do not lose the game."""

    name: str
    count: int = Field(ge=1, le=MAX_LIBRARY_FILLER)


class SpecPlayer(_Strict):
    name: str
    life: int = 20
    library_filler: LibraryFiller | None = None

    @field_validator("name")
    @classmethod
    def _name_is_path_safe(cls, value: str) -> str:
        return _check_name(value, "players.name")


class SpecCard(_Strict):
    card: str
    player: str
    zone: str = "hand"
    label: str | None = None
    tapped: bool = False
    face_up: bool = True
    position: str | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    damage: int = 0
    summoning_sick: bool = False


class SandboxSpec(_Strict):
    players: list[SpecPlayer] = Field(min_length=1)
    active_player: str | None = None
    step: str = "precombat_main"
    cards: list[SpecCard] = Field(default_factory=list)
    mana_pool: dict[str, str] = Field(default_factory=dict)

    @field_validator("mana_pool")
    @classmethod
    def _mana_pool_keys_are_path_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            _check_name(key, "mana_pool key")
        return value


def resolve_labels(value: Any, labels: dict[str, str]) -> Any:
    """`value` with every whole-string "@label", at any depth, replaced by its card id."""
    if isinstance(value, str) and value.startswith("@"):
        name = value[1:]
        if name not in labels:
            raise ValueError(f"unknown label @{name}")
        return labels[name]
    if isinstance(value, dict):
        return {k: resolve_labels(v, labels) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [resolve_labels(v, labels) for v in value]
    return value


@lru_cache(maxsize=1)
def _dry_run_templates() -> frozenset[str]:
    """Path templates of the POST routes whose request model has a `dry_run` field."""
    from magic_cards_model_api.main import app

    found: set[str] = set()
    for route in app.routes:
        body_field = getattr(route, "body_field", None)
        model_fields = (
            getattr(body_field.field_info.annotation, "model_fields", None)
            if (body_field is not None)
            else None
        )
        if model_fields and "dry_run" in model_fields:
            found.add(route.path)  # type: ignore[attr-defined]
    return frozenset(found)


def _card_entry(card: dict[str, Any], label_of: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": card["id"]}
    if card["id"] in label_of:
        out["label"] = label_of[card["id"]]
    out["name"] = card["name"]
    if card.get("face_up") is False:  # the admin view names it in card_name only
        out["name"] = card.get("card_name") or card["name"]
        out["face_down"] = True
    if card.get("tapped"):
        out["tapped"] = True
    if card.get("counters"):
        out["counters"] = card["counters"]
    if card.get("power") is not None:
        out["power"] = card["power"]
    if card.get("toughness") is not None:
        out["toughness"] = card["toughness"]
    if card.get("damage"):
        out["damage"] = card["damage"]
    return out


def compact_state(view: dict[str, Any], labels: dict[str, str] | None = None) -> dict[str, Any]:
    """The board in few tokens: cards by zone per player, labels where known."""
    label_of = {card_id: label for label, card_id in (labels or {}).items()}

    def cards(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_card_entry(c, label_of) for c in items]

    players: dict[str, Any] = {}
    for p in view["players"]:
        name = p["username"]
        players[name] = {
            "life": p["life"],
            "poison": p.get("counters", {}).get("poison", 0),
            "mana_pool": p["mana_pool"].get("floating", {}),
            "hand": cards(view["hands"].get(name, [])),
            "battlefield": cards([c for c in view["battlefield"] if c["controller"] == name]),
            "graveyard": cards(view["graveyards"].get(name, [])),
            "exile": cards([c for c in view["exile"] if c.get("owner", c["controller"]) == name]),
            "library_count": p["library_count"],
        }
    out: dict[str, Any] = {
        "turn": view["turn_number"],
        "step": view["step"],
        "active_player": view["active_player"],
        "priority_player": view["priority_holder"],
        "winner": view["winner"],
        "drawn": view["drawn"],
        "players": players,
        "stack": cards(view["stack"]),
        "combat": view["combat"],
        "pending_decision": view["pending_decision"],
    }
    if view.get("pregame"):
        out["pregame"] = view["pregame"]
    return out


def _all_card_names(view: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("battlefield", "stack", "exile"):
        names.update(c["name"] for c in view[key])
    for key in ("hands", "graveyards", "libraries"):
        for items in view.get(key, {}).values():
            names.update(c["name"] for c in items)
    return names


@dataclass(frozen=True)
class _Snapshot:
    """A saved copy of the game and its paused command (a pending decision lives there)."""

    label: str | None
    game: Any  # engine Game, never handed out: restore stores a fresh copy
    paused: Any
    turn: int
    step: str


def _refusal(reason: str) -> dict[str, Any]:
    return {"allowed": False, "reason": reason}


class SandboxGame:
    """One sandbox board. `http` is an httpx-compatible client (`TestClient(app)`)."""

    def __init__(self, http: Any, max_snapshots: int = DEFAULT_MAX_SNAPSHOTS) -> None:
        self._http = http
        self._max_snapshots = max_snapshots
        self._snapshots: dict[str, _Snapshot] = {}
        self._snapshot_seq = 0
        self._game_id: str | None = None
        self._admin_key = ""
        self._seat_uids: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        # Play mode: the chat holds one seat, the baseline plays the other.
        self._me: str | None = None
        self._opponent: Any = None  # BaselineSeat
        self._seam: Any = None  # HttpSeatApiClient over the same client
        self._log_mark = 0  # last log seq the chat has been told about
        self._deck_names: set[str] = set()

    @property
    def play_mode(self) -> bool:
        return self._me is not None

    # -- plumbing ---------------------------------------------------------

    def _secrets(self) -> list[str]:
        return [self._admin_key, *self._seat_uids.values()]

    def _request(
        self,
        method: str,
        template: str,
        *,
        uid: str | None = None,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        **path: str,
    ) -> tuple[int, Any]:
        for key, value in path.items():
            _check_name(value, key)
        quoted = {k: quote(v, safe="") for k, v in path.items()}
        url = template.format(game_id=self._game_id, **quoted)
        params = {**(query or {}), **({"uid": uid} if uid else {})}
        resp = self._http.request(method, url, params=params, json=body)
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": resp.text}
        return resp.status_code, data

    def _admin(self, method: str, template: str, body: dict[str, Any] | None = None, **path: str):
        status, data = self._request(method, template, uid=self._admin_key, body=body, **path)
        if status >= 400:
            raise ValueError(f"{method} {template} failed ({status}): {_error_text(data)}")
        return data

    def _require_game(self) -> None:
        if self._game_id is None:
            raise RuntimeError("no sandbox game: call create first")

    def _view(self) -> dict[str, Any]:
        """The admin board, or in play mode the chat seat's own view (opponent hand hidden)."""
        if self._me is None:
            return self._admin("GET", _G)
        status, data = self._request("GET", _G, uid=self._seat_uids[self._me])
        if status >= 400:
            raise ValueError(f"GET {_G} failed ({status}): {_error_text(data)}")
        return data

    # -- public -----------------------------------------------------------

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        parsed = SandboxSpec.model_validate(spec)
        self.close()
        status, data = self._request(
            "POST",
            "/api/games",
            body={
                "players": [{"username": p.name, "life": p.life} for p in parsed.players],
                "active_player": parsed.active_player,
                "step": parsed.step,
            },
        )
        if status >= 400:
            raise ValueError(f"could not create the game ({status}): {_error_text(data)}")
        self._game_id = data["game_id"]
        self._admin_key = data["admin_key"]
        self._seat_uids = dict(data["seat_uids"])
        self._labels = {}
        try:
            for card in parsed.cards:
                self._add_spec_card(card)
            for player in parsed.players:
                if (filler := player.library_filler) is None:
                    continue
                spare = SpecCard(
                    card=filler.name, player=player.name, zone="library", position="bottom"
                )
                for _ in range(filler.count):
                    self._add_spec_card(spare)
            for player, mana in parsed.mana_pool.items():
                floating: dict[str, int] = {}
                for color in mana.upper():
                    floating[color] = floating.get(color, 0) + 1
                self._admin("PUT", ACTIONS["mana_pool"][1], {"set": floating}, username=player)
        except Exception:
            self.close()
            raise
        return self.answer()

    def _add_spec_card(self, card: SpecCard) -> None:
        added = self._admin(
            "POST",
            ACTIONS["add_card"][1],
            {
                "card": card.card,
                "player": card.player,
                "zone": card.zone,
                "position": card.position,
                "tapped": card.tapped,
                "face_up": card.face_up,
            },
        )["added"]
        card_id = added["card_id"]
        if card.label:
            if card.label in self._labels:
                raise ValueError(f"duplicate label {card.label}")
            self._labels[card.label] = card_id
        patch: dict[str, Any] = {}
        if card.counters:
            patch["counters"] = card.counters
        if card.damage:
            patch["damage"] = card.damage
        if "summoning_sick" in card.model_fields_set:
            patch["summoning_sick"] = card.summoning_sick
        if patch:
            self._admin("PATCH", ACTIONS["patch_card"][1], patch, card_id=card_id)

    def state(self, full: bool = False) -> dict[str, Any]:
        self._require_game()
        view = self._view()
        out = self.answer(view=view)
        if full:
            out["state"] = redact(view, self._secrets())
        return out

    def legal_actions(self, player: str) -> dict[str, Any]:
        self._require_game()
        if player not in self._seat_uids:
            return self.answer(_refusal(f"unknown player {player}"))
        if self._me is not None and player != self._me:
            return self.answer(_refusal(f"play mode: you play {self._me}; only your own actions"))
        status, data = self._request(
            "GET", f"{_G}/legal-actions", uid=self._seat_uids[player], query={"player": player}
        )
        if status >= 400:
            return self.answer(_refusal(_error_text(data)))
        rows = [
            {k: v for k, v in row.items() if v not in (None, [], {})}
            for row in data.get("actions", [])
        ]
        return self.answer({**{k: v for k, v in data.items() if k != "actions"}, "actions": rows})

    def act(
        self, action: str, player: str | None, body: dict[str, Any], dry_run: bool = False
    ) -> dict[str, Any]:
        self._require_game()
        route = ACTIONS.get(action)
        if route is None:
            return self.answer(_refusal(f"unknown action {action}; known: {', '.join(ACTIONS)}"))
        method, template, credential = route
        if self._me is not None:
            if credential == "admin" and action != "advance":
                return self.answer(_refusal(PLAY_REFUSAL))
            # `advance` is admin-routed but is the seat's own way out of untap and cleanup
            # (the baseline uses it too), so it stays allowed under the chat's own seat uid.
            # Every other admin route (setup, engine actions) would cheat and is refused.
            credential = "seat"
            player = player or self._me
            if player != self._me:
                return self.answer(_refusal(f"play mode: you play {self._me}, not {player}"))
        if dry_run and template not in _dry_run_templates():
            return self.answer(_refusal(f"dry_run is not supported for {action}"))
        try:
            payload = resolve_labels(dict(body), self._labels)
        except ValueError as exc:
            return self.answer(_refusal(str(exc)))
        # add_card may name the new card like a spec card does, so later steps can address it
        label = payload.pop("label", None) if action == "add_card" else None
        if label is not None and label in self._labels:
            return self.answer(_refusal(f"duplicate label {label}"))
        path: dict[str, str] = {}
        if "{card_id}" in template:
            path["card_id"] = str(payload.pop("card_id", ""))
        if "{username}" in template:
            path["username"] = str(payload.pop("player", player or ""))
        for key, value in path.items():
            if not _safe_path_value(value):
                reason = f"invalid {key} {value!r}: not a card id or player name"
                return self.answer(_refusal(reason))
        if player is not None and credential == "seat":
            payload.setdefault("player", player)
        if dry_run:
            payload["dry_run"] = True
        if credential == "admin":
            uid = self._admin_key
            if action == "advance" and "player" not in payload:
                payload["player"] = self._view()["active_player"]
        else:
            uid = self._seat_uids.get(player or "")
            if uid is None:
                return self.answer(_refusal(f"{action} needs a known player, got {player!r}"))
        status, data = self._request(method, template, uid=uid, body=payload, **path)
        if status >= 400:
            return self.answer(_refusal(_error_text(data)))
        if isinstance(data, dict) and "players" in data and "battlefield" in data:
            data = {k: v for k, v in data.items() if k not in _VIEW_FIELDS}
        if label is not None:
            self._labels[str(label)] = data["added"]["card_id"]
        if self._me is not None:
            self._log_mark = self._read_log(self._log_mark)[1]  # the chat's own entries
        return self.answer(data)

    def close(self) -> None:
        if self._game_id is None:
            return
        if self._me is not None:
            from magic_cards_model_api.store import store

            store.delete(self._game_id)
        else:
            self._request("DELETE", _G, query={"key": self._admin_key})
        self._game_id = None
        self._labels = {}
        self._snapshots = {}
        self._snapshot_seq = 0
        self._me = None
        self._opponent = None
        self._seam = None
        self._seat_uids = {}
        self._deck_names = set()

    def answer(self, result: Any = None, view: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_game()
        view = view if view is not None else self._view()
        state = compact_state(view, self._labels)
        if self._me is not None:
            counts = {p["username"]: p["hand_count"] for p in view["players"]}
            for name, board in state["players"].items():
                if name != self._me:  # the seat view holds no opponent hand: show the count
                    del board["hand"]
                    board["hand_count"] = counts[name]
        out = {
            "result": redact(result, self._secrets()),
            "state": redact(state, self._secrets()),
            "labels": dict(self._labels),
            **self._support(view),
        }
        if self._snapshots:
            out["snapshots"] = [
                {"id": sid, "label": snap.label, "turn": snap.turn, "step": snap.step}
                for sid, snap in self._snapshots.items()
            ]
        return out

    # -- snapshots (board mode) ---------------------------------------------

    def snapshot(self, label: str | None = None) -> dict[str, Any]:
        """Save the board under a new id (or replace the snapshot that has `label`)."""
        from magic_cards_model_api.engine_api import copy_game
        from magic_cards_model_api.store import store

        self._require_game()
        if label is not None:
            _check_name(label, "label")
            if re.fullmatch(r"s\d+", label):
                raise ValueError(f"invalid label {label!r}: it looks like a snapshot id")
        if self._me is not None:
            return self.answer(_refusal(SNAPSHOT_PLAY_REFUSAL))
        sid = next((k for k, v in self._snapshots.items() if label and v.label == label), None)
        if sid is None and len(self._snapshots) >= self._max_snapshots:
            return self.answer(
                _refusal(
                    f"snapshot cap of {self._max_snapshots} reached (MTG_SIM_MAX_SNAPSHOTS):"
                    " reuse a label to replace a snapshot, or start a new sandbox"
                )
            )
        view = self._view()
        with store.locked(self._game_id) as game:
            saved = copy_game(game)
            paused = copy.deepcopy(store.paused_command(self._game_id))
        if sid is None:
            self._snapshot_seq += 1
            sid = f"s{self._snapshot_seq}"
        self._snapshots[sid] = _Snapshot(label, saved, paused, view["turn_number"], view["step"])
        return self.answer({"snapshot": sid, "label": label}, view=view)

    def restore(self, snapshot: str) -> dict[str, Any]:
        """Put a fresh copy of a snapshot back as the game; the snapshot stays reusable."""
        from magic_cards_model_api.engine_api import copy_game
        from magic_cards_model_api.store import store

        self._require_game()
        if self._me is not None:
            return self.answer(_refusal(SNAPSHOT_PLAY_REFUSAL))
        sid = snapshot if snapshot in self._snapshots else None
        sid = sid or next((k for k, v in self._snapshots.items() if v.label == snapshot), None)
        if sid is None:
            known = ", ".join(self._snapshots) or "none"
            return self.answer(_refusal(f"unknown snapshot {snapshot!r}; known: {known}"))
        snap = self._snapshots[sid]
        with store.locked(self._game_id):
            store.replace(self._game_id, copy_game(snap.game))
            if snap.paused is None:
                store.clear_paused_command(self._game_id)
            else:
                store.store_paused_command(self._game_id, *copy.deepcopy(snap.paused))
        return self.answer({"restored": sid, "label": snap.label})

    def _support(self, view: dict[str, Any]) -> dict[str, Any]:
        names = _all_card_names(view)
        if self._me is None:
            return support_block(names)
        try:  # both whole decks, not only the cards in sight: the chat chose them
            return {"support_status": "ok", **support_block(names | self._deck_names)}
        except Exception:  # noqa: BLE001 - advisory; say so instead of failing the move
            return support_unknown("unavailable")

    # -- play mode ----------------------------------------------------------

    def create_play(
        self,
        deck_me: str,
        deck_opponent: str,
        me: str = "you",
        opponent: str = "baseline",
        seed: int | None = None,
        me_first: bool | None = None,
    ) -> dict[str, Any]:
        """Start a game from two decklists: the chat plays `me`, the baseline plays `opponent`.

        The boot is `serve._boot_from_spec`'s, from text. `me_first=None` lets the seed pick
        the seat order.
        """
        from magic_cards_model_api.baseline_seat import BaselineSeat
        from magic_cards_model_api.decks import parse_decklist
        from magic_cards_model_api.sim.match import boot_baseline_game

        _check_name(me, "me")
        _check_name(opponent, "opponent")
        if me == opponent:
            raise ValueError("me and opponent must be different names")
        self.close()
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)
        if me_first is None:
            me_first = random.Random(seed).random() < 0.5
        lists = [(me, deck_me), (opponent, deck_opponent)]
        if not me_first:
            lists.reverse()
        self._game_id, self._seat_uids, self._seam = boot_baseline_game(self._http, lists, seed)
        self._me = me
        uid = self._seat_uids[opponent]
        self._opponent = BaselineSeat(self._seam, self._game_id, opponent, uid)
        self._log_mark = 0
        self._deck_names = set()
        for text in (deck_me, deck_opponent):
            main, side = parse_decklist(text)
            self._deck_names.update(card for _, card in main + side)
        first = me if me_first else opponent
        return self.answer({"you": me, "opponent": opponent, "seed": seed, "first_player": first})

    def _read_log(self, since: int) -> tuple[list[dict[str, Any]], int]:
        status, data = self._request(
            "GET", f"{_G}/log", uid=self._seat_uids[self._me or ""], query={"since": since}
        )
        if status >= 400:
            return [], since
        entries = [e for e in data.get("log", []) if isinstance(e, dict)]
        return entries, int(data.get("last_seq", since))

    def play_status(self) -> dict[str, Any]:
        """`your_turn` and `game_over` from the chat seat's view."""
        from magic_cards_model_api.baseline_seat import has_turn

        self._require_game()
        state = self._view()
        over = bool(state.get("winner") or state.get("drawn"))
        return {"your_turn": not over and has_turn(state, self._me or ""), "game_over": over}

    def advance_opponent(self, budget_s: float) -> dict[str, Any]:
        """Let the baseline play until it is the chat's move, the game ends or time runs out."""
        from magic_cards_model_api.baseline_seat import STALL_ROUNDS, has_turn
        from magic_cards_model_api.seatkit import log_headline

        self._require_game()
        deadline = time.monotonic() + budget_s
        note: str | None = None
        idle = 0
        while True:
            state = self._seam.game_state(self._game_id, self._seat_uids[self._me])
            if state.get("winner") or state.get("drawn") or has_turn(state, self._me):
                break
            if time.monotonic() >= deadline:
                note = OPPONENT_STILL_PLAYING
                break
            self._opponent.step()
            idle = 0 if self._opponent.advanced else idle + 1
            if idle >= STALL_ROUNDS:
                note = NO_PROGRESS
                break
        entries, self._log_mark = self._read_log(self._log_mark)
        moves = [log_headline(e) for e in entries]
        out: dict[str, Any] = {**self.play_status(), "opponent_moves": moves[-_MAX_OPPONENT_MOVES:]}
        if len(moves) > _MAX_OPPONENT_MOVES:
            out["opponent_moves_omitted"] = len(moves) - _MAX_OPPONENT_MOVES
        if note:
            out["note"] = note
        return out

    def settle_play(
        self, out: dict[str, Any], budget_s: float, advance: bool = True
    ) -> dict[str, Any]:
        """`out` after the opponent played on, plus `your_turn`, `opponent_moves`, `game_over`.

        Skipped (flags only) when the chat's action was refused: nothing moved.
        """
        if not advance:
            return {**out, **self.play_status(), "opponent_moves": []}
        try:
            adv = self.advance_opponent(budget_s)
            return {**self.answer(out["result"]), **adv}
        except Exception as exc:  # noqa: BLE001 - the accepted action must not read as refused
            reason = f"opponent could not continue ({type(exc).__name__}); call sim_state"
            try:
                status = self.play_status()
            except Exception:  # noqa: BLE001 - status is best effort here
                status = {}
            note = redact(reason, self._secrets())
            return {**out, **status, "opponent_moves": [], "note": note}


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        detail = data.get("detail", data.get("reason", data))
        return detail if isinstance(detail, str) else str(detail)
    return str(data)
