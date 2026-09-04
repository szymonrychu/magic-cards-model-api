from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node


def creature(id, controller, **over):
    c = {
        "id": id,
        "name": id,
        "controller": controller,
        "tapped": False,
        "summoning_sick": False,
        "type_line": "Creature",
        "zone": "battlefield",
        "abilities": [],
    }
    c.update(over)
    return c


STATE = {
    "you": "alice",
    "active_player": "alice",
    "step": "declare_attackers",
    "players": [{"username": "alice"}, {"username": "bob"}],
    "battlefield": [
        creature("a1", "alice"),
        creature("a2", "alice", tapped=True),
        creature("a3", "alice", summoning_sick=True),
        creature("pw", "bob", type_line="Legendary Planeswalker"),
        creature("b1", "bob"),
    ],
    "combat": {"attacks": []},
}


def test_attack_candidates_and_defenders() -> None:
    result = run_module(
        "combat.js",
        "[m.attackCandidates(s).map(c => c.id), m.defenders(s).map(d => d.id)]",
        s=STATE,
    )
    assert result == [["a1"], ["bob", "pw"]]


def test_attack_body_from_toggles() -> None:
    body = run_module(
        "combat.js",
        "m.attackBody(m.toggleAttacker(m.toggleAttacker({}, 'a1', 'bob'), 'a3', 'pw'))",
    )
    assert body == {
        "attackers": [
            {"card_id": "a1", "defender": "bob"},
            {"card_id": "a3", "defender": "pw"},
        ]
    }
    assert run_module(
        "combat.js",
        "m.attackBody(m.toggleAttacker(m.toggleAttacker({}, 'a1', 'bob'), 'a1', 'bob'))",
    ) == {"attackers": []}


def test_block_body_from_toggles() -> None:
    state = {
        **STATE,
        "active_player": "bob",
        "step": "declare_blockers",
        "combat": {"attacks": [{"attacker": "b1", "defender": "alice", "blockers": []}]},
    }
    body = run_module("combat.js", "m.blockBody(m.toggleBlock({}, 'a1', 'b1'), 'alice')", s=state)
    assert body == {"player": "alice", "blocks": [{"blocker": "a1", "blocked": "b1"}]}
    assert run_module("combat.js", "m.attacksAgainstYou(s).map(a => a.attacker)", s=state) == ["b1"]
