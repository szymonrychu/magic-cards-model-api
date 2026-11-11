"""A sandbox game built and driven through this API's own HTTP routes.

Runs inside the sim worker over `TestClient(app)`. The admin key and the seat uids
never leave this object: every answer is redacted, and cards are addressed by labels
("@bolt") the spec gave them instead of ids.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from magic_cards_model_api.schemas import GameStateView
from magic_cards_model_api.sim.redact import redact, support_block

Credential = Literal["seat", "admin"]
ActionRoute = tuple[str, str, Credential]

_G = "/api/games/{game_id}"
_PATH_PARAM = re.compile(r"[A-Za-z0-9_.:-]+")

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

_VIEW_FIELDS = frozenset(GameStateView.model_fields)
_ZONES = ("hands", "graveyards")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecPlayer(_Strict):
    name: str
    life: int = 20


class SpecCard(_Strict):
    card: str
    player: str
    zone: str = "hand"
    label: str | None = None
    tapped: bool = False
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
    return {
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


def _all_card_names(view: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("battlefield", "stack", "exile"):
        names.update(c["name"] for c in view[key])
    for key in ("hands", "graveyards", "libraries"):
        for items in view.get(key, {}).values():
            names.update(c["name"] for c in items)
    return names


def _refusal(reason: str) -> dict[str, Any]:
    return {"allowed": False, "reason": reason}


class SandboxGame:
    """One sandbox board. `http` is an httpx-compatible client (`TestClient(app)`)."""

    def __init__(self, http: Any) -> None:
        self._http = http
        self._game_id: str | None = None
        self._admin_key = ""
        self._seat_uids: dict[str, str] = {}
        self._labels: dict[str, str] = {}

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
        return self._admin("GET", _G)

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
        if dry_run and template not in _dry_run_templates():
            return self.answer(_refusal(f"dry_run is not supported for {action}"))
        try:
            payload = resolve_labels(dict(body), self._labels)
        except ValueError as exc:
            return self.answer(_refusal(str(exc)))
        path: dict[str, str] = {}
        if "{card_id}" in template:
            path["card_id"] = str(payload.pop("card_id", ""))
        if "{username}" in template:
            path["username"] = str(payload.pop("player", player or ""))
        for key, value in path.items():
            if not _PATH_PARAM.fullmatch(value) or value in (".", ".."):
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
        return self.answer(data)

    def close(self) -> None:
        if self._game_id is None:
            return
        self._request("DELETE", _G, query={"key": self._admin_key})
        self._game_id = None
        self._labels = {}

    def answer(self, result: Any = None, view: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_game()
        view = view if view is not None else self._view()
        return {
            "result": redact(result, self._secrets()),
            "state": redact(compact_state(view, self._labels), self._secrets()),
            "labels": dict(self._labels),
            **support_block(_all_card_names(view)),
        }


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        detail = data.get("detail", data.get("reason", data))
        return detail if isinstance(detail, str) else str(detail)
    return str(data)
