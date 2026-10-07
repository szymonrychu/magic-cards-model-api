"""The play UI offers level up (CR 702.87a) and reconfigure (CR 702.151a).

Equip's own shape (`test_ui_app.py`'s equip block): neither keyword has an
entry in `card.abilities`, so whether either belongs in a permanent's menu is
answered by the engine's own `GET .../legal-actions` rows (`kind: level_up`,
`kind: reconfigure`, the unattach row marked `unattach: true`), never by the
card alone. Level up and the unattach post at once; the reconfigure attach
first asks which of the row's own candidates to attach to.
"""

from __future__ import annotations

from typing import Any

from test_ui_app import _click, _click_state, base_app, base_state, run_app
from test_ui_menus import call
from ui_node import requires_node

pytestmark = requires_node

LEVELER = {
    "id": "lv1",
    "name": "Lord of Shatterskull Pass",
    "type_line": "Creature - Minotaur Shaman",
    "zone": "battlefield",
    "controller": "alice",
    "keywords": ["Level Up"],
    "abilities": [],
}

SLING = {
    "id": "sl1",
    "name": "Simian Sling",
    "type_line": "Artifact Creature - Equipment Monkey",
    "zone": "battlefield",
    "controller": "alice",
    "keywords": ["Reconfigure"],
    "abilities": [],
}

BEARS = {
    "id": "bears1",
    "name": "Grizzly Bears",
    "type_line": "Creature - Bear",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [],
}


def _level_up_row() -> dict[str, Any]:
    return {
        "card_id": "lv1",
        "kind": "level_up",
        "ability_index": None,
        "keyword": "Level Up",
        "rule": "CR 702.87a",
        "cost": "{1}{R}",
        "affordable": "yes",
    }


def _attach_row() -> dict[str, Any]:
    return {
        "card_id": "sl1",
        "kind": "reconfigure",
        "ability_index": None,
        "keyword": "Reconfigure",
        "rule": "CR 702.151a",
        "cost": "{2}",
        "affordable": "yes",
        "target_slots": [{"minimum": 1, "maximum": 1}],
        "targets": {
            "understood": True,
            "slots": [
                {
                    "slot": 0,
                    "count": 1,
                    "candidates": ["bears1 Grizzly Bears (battlefield, alice)"],
                }
            ],
        },
    }


def _unattach_row() -> dict[str, Any]:
    return {
        "card_id": "sl1",
        "kind": "reconfigure",
        "ability_index": None,
        "keyword": "Reconfigure",
        "rule": "CR 702.151a",
        "cost": "{2}",
        "affordable": "yes",
        "unattach": True,
    }


RESPOND = """
  globalThis.__respond = (method, path) => {
    if (method === "GET" && path.includes("legal-actions")) {
      return Promise.resolve({ ok: true, json: async () => ({ actions: rows, last_seq: 7 }) });
    }
    if (method === "POST" && path.includes("actions/")) {
      return Promise.resolve({ ok: true, json: async () => answer });
    }
    return null;
  };
"""


def _posts(calls: list[dict[str, Any]], route: str) -> list[dict[str, Any]]:
    return [c for c in calls if c["method"] == "POST" and f"actions/{route}" in c["path"]]


# -- planPermanentClick --------------------------------------------------------


def test_a_leveler_and_a_reconfigure_permanent_route_to_a_menu() -> None:
    """Simian Sling is an Equipment too, so it carries Equip's own routing
    flag beside its reconfigure one; neither decides what the menu offers."""
    expected = (
        (LEVELER, {"levelUp": True}),
        (SLING, {"equipment": True, "reconfigure": True}),
    )
    for card, flags in expected:
        plan = run_app(
            "m.planPermanentClick(card, app, state)",
            card=card,
            app=base_app(),
            state=base_state(),
        )
        assert plan == {"kind": "menu", "abilities": [], "vehicle": False, **flags}, plan


# -- abilityMenuItems ----------------------------------------------------------


def test_the_menu_items_name_their_rows() -> None:
    plan = {"kind": "menu", "abilities": [], "vehicle": False, "levelUp": True}
    assert call("m.abilityMenuItems(plan, card)", plan=plan, card=LEVELER) == [
        {
            "key": "level_up",
            "label": "Level up",
            "sublabel": "",
            "match": {"card_id": "lv1", "kind": "level_up", "ability_index": None},
        }
    ]
    plan = {"kind": "menu", "abilities": [], "vehicle": False, "attach": True, "unattach": True}
    assert call("m.abilityMenuItems(plan, card)", plan=plan, card=SLING) == [
        {
            "key": "reconfigure",
            "label": "Reconfigure",
            "sublabel": "",
            "match": {"card_id": "sl1", "kind": "reconfigure", "ability_index": None},
        },
        {
            "key": "unattach",
            "label": "Unattach",
            "sublabel": "",
            "match": {
                "card_id": "sl1",
                "kind": "reconfigure",
                "ability_index": None,
                "unattach": True,
            },
        },
    ]


def test_the_attach_and_unattach_items_are_enriched_by_their_own_rows() -> None:
    """The two reconfigure rows share card and kind; `unattach` is what tells
    them apart, so an unaffordable unattach greys out only its own item."""
    plan = {"kind": "menu", "abilities": [], "vehicle": False, "attach": True, "unattach": True}
    unaffordable = {**_unattach_row(), "affordable": "no", "unaffordable_reason": "short {2}"}
    items = call(
        "m.enrichItems(m.abilityMenuItems(plan, card), rows)",
        plan=plan,
        card=SLING,
        rows=[_attach_row(), unaffordable],
    )
    assert [bool(i.get("disabled")) for i in items] == [False, True], items


def test_reconfigure_target_menu_items_are_one_per_candidate_line() -> None:
    lines = ["bears1 Grizzly Bears (battlefield, alice)"]
    assert call("m.reconfigureTargetMenuItems(lines)", lines=lines) == [
        {"key": "target:bears1", "label": "Attach to Grizzly Bears", "sublabel": "", "match": None}
    ]


# -- onPermanent: offer, post, refuse -----------------------------------------


def test_level_up_is_offered_from_its_row_and_posts_the_route() -> None:
    result = _click(
        RESPOND
        + """
          await m.onPermanent(leveler, { currentTarget: anchor });
          const labels = options().map((o) => o.label);
          options()[0].click();
          await settle();
          return { labels, calls: globalThis.__calls };
        """,
        state=_click_state(battlefield=[LEVELER]),
        leveler=LEVELER,
        rows=[_level_up_row()],
        answer={"allowed": True, "cost": "{1}{R}"},
    )
    assert result["labels"] == ["Level up"]
    (post,) = _posts(result["calls"], "level-up")
    assert post["body"] == {"player": "alice", "card_id": "lv1", "expected_seq": 7}


def test_level_up_is_not_offered_without_its_row() -> None:
    result = _click(
        RESPOND
        + """
          await m.onPermanent(leveler, { currentTarget: anchor });
          return { hidden: globalThis.__popover.hidden, options: options().length };
        """,
        state=_click_state(battlefield=[LEVELER]),
        leveler=LEVELER,
        rows=[],
        answer={},
    )
    assert result["hidden"] is True
    assert result["options"] == 0


def test_reconfigure_asks_for_the_target_then_posts_it() -> None:
    result = _click(
        RESPOND
        + """
          await m.onPermanent(sling, { currentTarget: anchor });
          const first = options().map((o) => o.label);
          options()[0].click();
          await settle();
          const second = options().map((o) => o.label);
          const before = globalThis.__calls.filter((c) => c.method === "POST").length;
          options()[0].click();
          await settle();
          return { first, second, before, calls: globalThis.__calls };
        """,
        state=_click_state(battlefield=[SLING, BEARS]),
        sling=SLING,
        rows=[_attach_row()],
        answer={"allowed": True, "cost": "{2}"},
    )
    assert result["first"] == ["Reconfigure"]
    assert result["second"] == ["Attach to Grizzly Bears"]
    assert result["before"] == 0
    (post,) = _posts(result["calls"], "reconfigure")
    assert post["body"] == {
        "player": "alice",
        "card_id": "sl1",
        "target": "bears1",
        "expected_seq": 7,
    }


def test_unattach_posts_the_route_with_no_target() -> None:
    result = _click(
        RESPOND
        + """
          await m.onPermanent(sling, { currentTarget: anchor });
          const labels = options().map((o) => o.label);
          options()[1].click();
          await settle();
          return { labels, calls: globalThis.__calls };
        """,
        state=_click_state(battlefield=[SLING, BEARS]),
        sling=SLING,
        rows=[_attach_row(), _unattach_row()],
        answer={"allowed": True, "cost": "{2}"},
    )
    assert result["labels"] == ["Reconfigure", "Unattach"]
    (post,) = _posts(result["calls"], "reconfigure")
    assert post["body"] == {"player": "alice", "card_id": "sl1", "expected_seq": 7}


def test_a_level_up_refusal_shows_the_engines_reason() -> None:
    reason = "Lord of Shatterskull Pass's Level Up ability can only be activated as a sorcery"
    result = _click(
        RESPOND
        + """
          await m.onPermanent(leveler, { currentTarget: anchor });
          options()[0].click();
          await settle();
          return { toast: globalThis.__toast.textContent, hidden: globalThis.__toast.hidden };
        """,
        state=_click_state(battlefield=[LEVELER]),
        leveler=LEVELER,
        rows=[_level_up_row()],
        answer={"allowed": False, "reason": reason},
    )
    assert result["hidden"] is False
    assert result["toast"] == reason
