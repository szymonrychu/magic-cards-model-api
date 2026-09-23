"""A modal spell built for the tests alone, with one mode no engine parser
can ever read.

`engine_api.unreadable_modes` (battle 3, B3-05) refuses a cast choosing a
mode the engine cannot read, and the tests proving that need a card with
such a mode. Real cards kept standing in for it - Thraben Charm's mode 3,
Cryptic Command's mode 3 - and each one broke the day the engine learned to
read it: nothing in the api changed, the engine simply got better. Under the
owner rule every Modern, Pioneer and Standard card ends up fully read, and
the pool's out-of-format modal cards with an unread mode are down to one, so
no real card is a stable vehicle.

`UNREADABLE_CHARM_RAW` is that vehicle: a real `Card` shape whose modes 1
and 2 are ordinary templating the engine reads, and whose mode 3 is a made-up
sentence no parser recognises. `test_the_unreadable_charm_is_still_unreadable`
(in `test_battle3_action_rows.py`) guards it, so a future reader that starts
parsing gibberish fails loudly instead of quietly invalidating every test that
relies on it.

It reaches a game through the exact seam a real card name resolves through -
`scryfall.fetch_card`, which `POST /api/games/{id}/cards` calls - via the
`unreadable_charm` fixture in `conftest.py`; the engine's own modal reading,
cast guard and resolution are never patched.
"""

from __future__ import annotations

from typing import Any

UNREADABLE_CHARM_NAME = "Garbled Charm"

#: The one mode of `UNREADABLE_CHARM_RAW` the engine cannot read.
UNREADABLE_MODE = 3
UNREADABLE_MODE_TEXT = "Each player frobnicates a zorblax."

UNREADABLE_CHARM_RAW: dict[str, Any] = {
    "id": "ffffffff-0000-0000-0000-000000000011",
    "oracle_id": "ffffffff-0000-0000-0000-000000000012",
    "name": UNREADABLE_CHARM_NAME,
    "type_line": "Instant",
    "mana_cost": "{1}{R}",
    "cmc": 2.0,
    "oracle_text": (
        "Choose one or more —\n"
        "• Garbled Charm deals 2 damage to any target.\n"
        "• Destroy target enchantment.\n"
        f"• {UNREADABLE_MODE_TEXT}"
    ),
    "colors": ["R"],
    "color_identity": ["R"],
    "keywords": [],
    "layout": "normal",
    "legalities": {"standard": "legal", "pioneer": "legal", "modern": "legal"},
    "rarity": "common",
    "set": "tst",
}
