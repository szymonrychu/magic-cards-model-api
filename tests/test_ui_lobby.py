"""`ui/render/lobby.js`'s `mountLobby` against a fake DOM and a stubbed global
`fetch` - no fixture server, no library, the same "run the real shipped
module under Node" approach every other `test_ui_*.py` file uses.

`mountLobby` had no tests of its own before this whole-branch review. The one
covered here is its most basic promise: `loadBundledDecks` fires a
`GET /api/decks` and appends one `<option>` per name it gets back to
*both* seat pickers (`#you-bundled` and `#ai-bundled`) - the two-select
fan-out is the part worth pinning, since a one-select test would not catch a
regression that only wired one of them.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node

# A DOM minimal enough for `lobby.js`'s own needs: it never goes through
# `dom.js`'s `h()` (no build step there either - `document.createElement`
# and direct `.value`/`.textContent` assignment, same as the classic script
# it was ported from), so this fake DOM stays plain rather than reusing the
# heavier `h()`-compatible one `test_ui_ribbon.py` needs - a getter-only
# `textContent` (that shape) would throw the moment `loadBundledDecks` does
# `opt.textContent = name` in strict-mode ES module code.
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.value = "";
    this.textContent = "";
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelector(sel) {
    if (!sel.startsWith("#")) return null;
    const id = sel.slice(1);
    const walk = (node) => {
      if (!node) return null;
      if (node.attrs && node.attrs.id === id) return node;
      for (const c of node.children || []) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
}

globalThis.document = new El("#document");
document.createElement = (t) => new El(t);

function boxed(id) {
  const el = document.createElement("select");
  el.setAttribute("id", id);
  document.appendChild(el);
  return el;
}
"""


GAMES = [
    {
        "game_id": "g1",
        "players": ["you", "bot"],
        "turn_number": 3,
        "phase": "precombat_main",
        "is_over": False,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        "seats": {"you": "u1"},
        "created_at": "2026-09-08T12:00:00+00:00",
    }
]
SAVED = [{"gameId": "g1", "uid": "u1", "aiUid": "u2"}]


def test_lobby_rows_join_server_games_with_saved_seats() -> None:
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES, saved=SAVED)
    assert rows == [
        {
            "gameId": "g1",
            "opponent": "bot",
            "turn": 3,
            "phase": "precombat_main",
            "over": False,
            "archived": False,
            "unloadable": None,
            "recordedEnding": None,
            "endedAt": None,
            "createdAt": "2026-09-08T12:00:00+00:00",
            "model": "opus",
            "effort": "max",
            "myUid": "u1",
            "aiUid": "u2",
        }
    ]


def test_agent_body_from_fields() -> None:
    assert run_module(
        "lobby_model.js", "m.agentFrom({ model: ' fable ', effort: 'low', thinking: '' })"
    ) == {"model": "fable", "effort": "low", "max_thinking_tokens": None}
    assert run_module(
        "lobby_model.js", "m.agentFrom({ model: '', effort: 'high', thinking: '0' })"
    ) == {"model": "sonnet", "effort": "high", "max_thinking_tokens": 0}


def test_agent_body_falls_back_to_medium_effort() -> None:
    """`medium`, not `high`: a live-pod audit found latency correlates almost
    perfectly with output tokens, and `high` effort - not context size - is
    what makes a seat slow. The per-game dropdown can still pick `high` for a
    combo-heavy deck; this is only what an omitted or unrecognised value
    falls back to."""
    assert (
        run_module("lobby_model.js", "m.agentFrom({ model: 'sonnet', thinking: '' })")["effort"]
        == "medium"
    )
    assert (
        run_module(
            "lobby_model.js",
            "m.agentFrom({ model: 'sonnet', effort: 'not-a-real-level', thinking: '' })",
        )["effort"]
        == "medium"
    )


def test_agent_body_rejects_a_fraction_or_a_negative_thinking_budget() -> None:
    """`AgentSettings.max_thinking_tokens` is `int | None = Field(ge=0)` - a
    fraction or a negative number is not a legal value for it, so `agentFrom`
    must send `null` rather than truncate or pass either one through."""
    assert (
        run_module("lobby_model.js", "m.agentFrom({ thinking: '5.5' })")["max_thinking_tokens"]
        is None
    )
    assert (
        run_module("lobby_model.js", "m.agentFrom({ thinking: '-1' })")["max_thinking_tokens"]
        is None
    )


GAMES_MIXED = [
    {
        "game_id": "g1",
        "players": ["you", "bot"],
        "turn_number": 3,
        "phase": "precombat_main",
        "is_over": False,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        # Both of this browser's own uids can land in `seats` here - the
        # human seat "you" (u1) and the AI seat "bot" (u2), since
        # `refreshGames` sends both uids in its `?uids=` query and the
        # server includes every `seats` entry whose uid is in that list.
        # "bot" is listed first on purpose - the row must still name "you"
        # as `me` (via the saved entry's own `uid`), not "whichever key
        # comes first".
        "seats": {"bot": "u2", "you": "u1"},
    },
    {
        # A game this browser never saved and holds no seat uid in -
        # `lobbyViewModel` must drop it, never fall back to treating
        # `players[0]` as "the opponent".
        "game_id": "g2",
        "players": ["stranger1", "stranger2"],
        "turn_number": 1,
        "phase": "beginning",
        "is_over": False,
        "agents": {},
        "seats": {},
    },
]
SAVED_MIXED = [{"gameId": "g1", "uid": "u1", "aiUid": "u2"}]


def test_lobby_view_model_keeps_only_owned_games_and_names_the_right_opponent() -> None:
    rows = run_module(
        "lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES_MIXED, saved=SAVED_MIXED
    )
    assert len(rows) == 1
    assert rows[0]["gameId"] == "g1"
    assert rows[0]["opponent"] == "bot"
    assert rows[0]["myUid"] == "u1"


def test_mount_lobby_populates_both_bundled_deck_selects() -> None:
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const you = boxed("you-bundled");
          const ai = boxed("ai-bundled");

          const fetchCalls = [];
          globalThis.fetch = async (path) => {
            fetchCalls.push(path);
            return {
              ok: true,
              status: 200,
              json: async () => ({ decks: ["Modern Burn", "Modern Affinity"] }),
            };
          };

          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {} });
          // `loadBundledDecks` is fired inside `mountLobby` without being
          // awaited (see that module's own comment on why) - give its
          // promise chain a tick to land before reading the selects back.
          await new Promise((resolve) => setTimeout(resolve, 20));

          return {
            fetchCalls,
            you: you.children.map((o) => ({ value: o.value, text: o.textContent })),
            ai: ai.children.map((o) => ({ value: o.value, text: o.textContent })),
          };
        })()
        """,
    )
    # `/api/lobby/games` rides along because `mountLobby` also refreshes the
    # games table, and it now asks the server even with `mcm.games.v1` empty
    # (it used to short-circuit to an empty table, which is exactly why a
    # second computer saw nothing - see `refreshGames`). The subject of this
    # test is the two deck selects; the game-list call is asserted properly
    # in `test_the_lobby_asks_the_server_even_with_nothing_saved` below.
    assert result["fetchCalls"] == ["/api/decks", "/api/lobby/games"]
    expected = [
        {"value": "Modern Burn", "text": "Modern Burn"},
        {"value": "Modern Affinity", "text": "Modern Affinity"},
    ]
    assert result["you"] == expected
    assert result["ai"] == expected


def test_lobby_table_renders_a_row_and_end_ends_the_game() -> None:
    """`mountLobby` reads `mcm.games.v1`, joins it against
    `GET /api/lobby/games?uids=...` through `lobbyViewModel`, and renders one
    `<tr>` per row into `#games-tbody`. Clicking a row's End button calls
    `DELETE /api/lobby/games/{id}?uid=<my seat uid>`.

    End used to *drop* the row from `mcm.games.v1`. It now keeps it, stamped
    `endedAt`, and the row re-renders as an ended one - because `DELETE` is a
    soft delete (`games.archived_at`) whose entire purpose is that a finished
    game stays reviewable, and `GET .../review` needs a seat uid that only
    this browser ever held. Dropping the entry made the server's archive
    unreachable forever. The row's own Forget button drops it when a person
    means to."""
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const table = document.createElement("table");
          table.setAttribute("id", "games-table");
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          table.appendChild(tbody);
          document.appendChild(table);

          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };

          const fetchCalls = [];
          globalThis.fetch = async (path, opts) => {
            const method = (opts && opts.method) || "GET";
            fetchCalls.push(`${method} ${path}`);
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            if (method === "DELETE") {
              return { ok: true, status: 200, json: async () => ({ deleted: true }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };

          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {} });
          await new Promise((resolve) => setTimeout(resolve, 20));

          const rowText = tbody.children[0].children.map((td) => td.textContent);
          const actionsCell = tbody.children[0].children[tbody.children[0].children.length - 1];
          const endBtn = actionsCell.children[1];
          endBtn.listeners.click();
          await new Promise((resolve) => setTimeout(resolve, 20));

          return {
            fetchCalls,
            rowText,
            rowCountAfter: tbody.children.length,
            savedAfter: JSON.parse(storageData["mcm.games.v1"] || "[]"),
          };
        })()
        """,
        games=GAMES,
        saved=SAVED,
    )
    assert "GET /api/lobby/games?uids=u1%2Cu2" in result["fetchCalls"]
    assert result["rowText"][0] == "bot"
    assert result["rowText"][5] == "playing"
    assert "DELETE /api/lobby/games/g1?uid=u1" in result["fetchCalls"]
    assert result["rowCountAfter"] == 1
    assert len(result["savedAfter"]) == 1
    kept = result["savedAfter"][0]
    assert kept["gameId"] == "g1"
    assert kept["uid"] == "u1", "the seat uid the review needs must survive End"
    assert kept["opponent"] == "bot"
    assert kept["endedAt"]


def test_lobby_end_keeps_the_row_when_the_server_refuses_the_delete() -> None:
    """A refused (non-2xx) `DELETE` must not drop the row from `mcm.games.v1`
    or the table - only a `DELETE` the server actually agreed to may do
    either. The refusal's message lands in `#start-error` instead."""
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const table = document.createElement("table");
          table.setAttribute("id", "games-table");
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          table.appendChild(tbody);
          document.appendChild(table);
          const errEl = document.createElement("div");
          errEl.setAttribute("id", "start-error");
          document.appendChild(errEl);

          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };

          const fetchCalls = [];
          globalThis.fetch = async (path, opts) => {
            const method = (opts && opts.method) || "GET";
            fetchCalls.push(`${method} ${path}`);
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            if (method === "DELETE") {
              return {
                ok: false,
                status: 404,
                json: async () => ({ detail: "that uid is no seat in this game" }),
              };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };

          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {} });
          await new Promise((resolve) => setTimeout(resolve, 20));

          const actionsCell = tbody.children[0].children[tbody.children[0].children.length - 1];
          const endBtn = actionsCell.children[1];
          endBtn.listeners.click();
          await new Promise((resolve) => setTimeout(resolve, 20));

          return {
            rowCountAfter: tbody.children.length,
            savedAfter: JSON.parse(storageData["mcm.games.v1"] || "[]"),
            errorText: errEl.textContent,
          };
        })()
        """,
        games=GAMES,
        saved=SAVED,
    )
    assert result["rowCountAfter"] == 1
    assert result["savedAfter"] == SAVED
    assert "that uid is no seat in this game" in result["errorText"]


# ------------------------------------------------------- the review's door --
#
# The post-game review is only reachable from the lobby, and only for a game
# that is finished - a live game's plans and reasoning are hidden information
# about how a seat intends to play, so the door itself is the first gate and
# the endpoint's own 409 is the second.

GAMES_OVER = [
    {
        "game_id": "g1",
        "players": ["you", "bot"],
        "turn_number": 11,
        "phase": "postcombat_main",
        "is_over": True,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        "seats": {"you": "u1"},
    }
]


def test_a_game_the_server_no_longer_lists_stays_in_the_table_as_an_ended_one() -> None:
    """`DELETE /api/lobby/games/{id}` soft-deletes (`games.archived_at`) so a
    finished game stays reviewable - which is worth nothing if the browser
    throws away the seat uid the review needs. An ended game keeps its row,
    marked, with the opponent's name it was saved with."""
    rows = run_module(
        "lobby_model.js",
        "m.lobbyViewModel(games, saved)",
        games=[],
        saved=[
            {
                "gameId": "g1",
                "uid": "u1",
                "aiUid": "u2",
                "endedAt": "2026-09-08T20:00:00Z",
                "opponent": "bot",
            }
        ],
    )
    assert len(rows) == 1
    assert rows[0]["gameId"] == "g1"
    assert rows[0]["archived"] is True
    assert rows[0]["over"] is True
    assert rows[0]["opponent"] == "bot"
    assert rows[0]["myUid"] == "u1"


def test_a_live_game_is_not_marked_archived() -> None:
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES, saved=SAVED)
    assert rows[0]["archived"] is False


def test_a_finished_game_offers_show_thinking_and_a_live_one_does_not() -> None:
    """The entry point, and the gate on it: the button exists only on a row
    the lobby already knows is over, and it hands the review the game's id
    and this browser's own seat uid."""
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          document.appendChild(tbody);

          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };
          globalThis.fetch = async (path) => {
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };

          const shown = [];
          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {}, showReview: (r) => shown.push(r) });
          await new Promise((resolve) => setTimeout(resolve, 20));

          const cell = tbody.children[0].children[tbody.children[0].children.length - 1];
          const labels = cell.children.map((b) => b.textContent);
          const review = cell.children.find((b) => b.textContent === "Show thinking");
          if (review) review.listeners.click();
          return { labels, shown };
        })()
        """,
        games=GAMES_OVER,
        saved=SAVED,
    )
    assert "Show thinking" in result["labels"]
    assert result["shown"] == [{"gameId": "g1", "uid": "u1"}]

    live = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          document.appendChild(tbody);
          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };
          globalThis.fetch = async (path) => {
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };
          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {}, showReview: () => {} });
          await new Promise((resolve) => setTimeout(resolve, 20));
          const cell = tbody.children[0].children[tbody.children[0].children.length - 1];
          return cell.children.map((b) => b.textContent);
        })()
        """,
        games=GAMES,
        saved=SAVED,
    )
    assert "Show thinking" not in live, (
        "a live game must never offer the review - it would hand one seat the other's plan mid-game"
    )


# A game the server lists but says this build cannot decode: `unloadable`
# carries the engine's own refusal, and every board-derived scalar is at its
# unknown value, exactly as `decks_api.LobbyGameSummary` describes.
GAMES_UNLOADABLE = [
    {
        "game_id": "g1",
        "players": ["you", "bot"],
        "turn_number": None,
        "phase": None,
        "is_over": False,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        "seats": {"you": "u1"},
        "unloadable": (
            "Game snapshot was written at schema_version=3, but this build "
            "decodes schema_version=5."
        ),
    }
]


def test_the_view_model_carries_the_reason_a_game_cannot_be_opened() -> None:
    rows = run_module(
        "lobby_model.js",
        "m.lobbyViewModel(games, saved)",
        games=GAMES_UNLOADABLE,
        saved=SAVED,
    )
    assert len(rows) == 1
    assert rows[0]["unloadable"] == GAMES_UNLOADABLE[0]["unloadable"]
    assert rows[0]["gameId"] == "g1"
    assert rows[0]["opponent"] == "bot", (
        "a game that cannot be decoded must still name who was playing it"
    )
    loadable = run_module(
        "lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES, saved=SAVED
    )
    assert loadable[0]["unloadable"] is None


def test_an_unopenable_game_offers_end_but_never_open() -> None:
    """Open would only ever get a 409 back (`app._game_unloadable`), and the
    one action that helps is End - which is also what frees the slot the row
    still counts against (`_live_ui_game_ids`)."""
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          document.appendChild(tbody);
          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };
          globalThis.fetch = async (path) => {
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };
          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {}, showReview: () => {} });
          await new Promise((resolve) => setTimeout(resolve, 20));
          const cells = tbody.children[0].children;
          const actions = cells[cells.length - 1];
          const stateCell = cells[cells.length - 2];
          return {
            labels: actions.children.map((b) => b.textContent),
            state: stateCell.textContent,
            title: stateCell.attrs.title || null,
          };
        })()
        """,
        games=GAMES_UNLOADABLE,
        saved=SAVED,
    )
    assert result["labels"] == ["End"], (
        "Open on a game this build cannot decode only ever answers 409 - offer End, "
        f"got {result['labels']}"
    )
    assert result["state"] == "unopenable"
    assert result["title"] == GAMES_UNLOADABLE[0]["unloadable"], (
        "the reason must be readable in the lobby, not only in the payload"
    )


# ---------------------------------------------------------------------------
# cross-device: the owner arrives on a second computer with empty storage
# ---------------------------------------------------------------------------
#
# "User must be able to start a game on one computer and finish on the other."
# The server half is `decks_api.list_lobby_games` handing the *owner* the full
# `seats` map (`tests/test_lobby_cross_device.py`); this is the browser half.
# A fresh browser has nothing in `mcm.games.v1`, so `saved` is empty and every
# uid in the row comes from the server - which means `lobbyViewModel` can no
# longer lean on a saved entry to know which of the two seats is the human's.

#: One server row as the *owner* now receives it: both seats, no saved entry
#: anywhere. "bot" is listed first on purpose - the human seat must be picked
#: because it is the one with no agent, never because of key order.
GAMES_OWNED = [
    {
        "game_id": "g1",
        "players": ["you", "bot"],
        "turn_number": 3,
        "phase": "precombat_main",
        "is_over": False,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        "seats": {"bot": "u2", "you": "u1"},
    }
]


def test_an_owned_row_with_nothing_saved_still_renders_and_is_playable() -> None:
    """The defect, at the view-model level: a second computer holds no saved
    entry for this game at all, so the row exists only because the server said
    the logged-in user owns it. It must render (not be filtered out as "some
    other browser's game") and must carry a real `myUid`, or Open is dead."""
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, [])", games=GAMES_OWNED)
    assert len(rows) == 1
    assert rows[0]["gameId"] == "g1"
    assert rows[0]["myUid"] == "u1", "the human seat's uid, not the AI's"
    assert rows[0]["aiUid"] == "u2"
    assert rows[0]["opponent"] == "bot"
    assert rows[0]["model"] == "opus"


def test_the_human_seat_is_the_one_with_no_agent_not_the_first_key() -> None:
    """How "my seat" is told from the AI's with nothing saved: the row already
    carries `agents`, keyed by player name, so the seat that is *not* in it is
    the human's. Key order is the server's dict iteration order and says
    nothing - here the AI seat comes first, and picking it would put the
    player in the bot's seat."""
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, [])", games=GAMES_OWNED)
    assert rows[0]["myUid"] != "u2"
    assert rows[0]["myUid"] == "u1"


HUMAN_VS_HUMAN = [
    {
        "game_id": "g1",
        "players": ["alice", "bob"],
        "turn_number": 1,
        "phase": "beginning",
        "is_over": False,
        # No AI seat at all: `agents` is empty, so "the seat with no agent"
        # matches both. This must still produce a usable row rather than
        # throw or hand back a null uid.
        "agents": {},
        "seats": {"alice": "u1", "bob": "u2"},
    }
]


def test_a_human_vs_human_owned_row_still_gets_a_uid_and_no_ai_uid() -> None:
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, [])", games=HUMAN_VS_HUMAN)
    assert len(rows) == 1
    assert rows[0]["myUid"] in ("u1", "u2")
    assert rows[0]["opponent"] in ("alice", "bob")
    assert rows[0]["aiUid"] is None, "there is no AI seat to (re)start in this game"


def test_a_saved_uid_still_wins_over_the_no_agent_heuristic() -> None:
    """The pre-existing rule is unchanged where a saved entry exists: this
    browser's own `uid` names its seat, even if that seat happens to be the
    AI's (a browser that started the game holds both uids)."""
    rows = run_module(
        "lobby_model.js",
        "m.lobbyViewModel(games, saved)",
        games=GAMES_OWNED,
        saved=[{"gameId": "g1", "uid": "u2", "aiUid": "u1"}],
    )
    assert rows[0]["myUid"] == "u2"
    assert rows[0]["aiUid"] == "u1"


def test_the_lobby_asks_the_server_even_with_nothing_saved() -> None:
    """`refreshGames` used to skip the network entirely when `mcm.games.v1`
    was empty and render an empty table - which is precisely why a second
    computer saw nothing. It must always ask, and with no `uids` parameter
    when it holds none, so the server can answer from the bearer token."""
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const table = document.createElement("table");
          table.setAttribute("id", "games-table");
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          table.appendChild(tbody);
          document.appendChild(table);

          const storageData = {};
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };

          const fetchCalls = [];
          globalThis.fetch = async (path, opts) => {
            const method = (opts && opts.method) || "GET";
            fetchCalls.push(`${method} ${path}`);
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };

          const opened = [];
          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: (t) => opened.push(t) });
          await new Promise((resolve) => setTimeout(resolve, 20));

          const actionsCell = tbody.children[0].children[tbody.children[0].children.length - 1];
          actionsCell.children[0].listeners.click();

          return { fetchCalls, rowCount: tbody.children.length, opened };
        })()
        """,
        games=GAMES_OWNED,
    )
    assert "GET /api/lobby/games" in result["fetchCalls"], (
        "with nothing saved the lobby must still ask the server, and without "
        f"a uids parameter - calls were {result['fetchCalls']}"
    )
    assert result["rowCount"] == 1, "the owner's row must render on a fresh browser"
    assert result["opened"] == [{"game_id": "g1", "uid": "u1", "aiUid": "u2"}], (
        "Open must hand the game screen the server-supplied uid"
    )


# The same row, for a game the ledger records as having ended
# (`decks_api.LobbyGameSummary.recorded_ending`). The board is still
# undecodable and still unopenable - but the review of it reads, rebuilt
# from that same ledger, so the door to it must be here.
GAMES_UNLOADABLE_ENDED = [{**GAMES_UNLOADABLE[0], "recorded_ending": 191}]


def test_the_view_model_carries_the_ledgers_record_of_an_ending() -> None:
    rows = run_module(
        "lobby_model.js",
        "m.lobbyViewModel(games, saved)",
        games=GAMES_UNLOADABLE_ENDED,
        saved=SAVED,
    )
    assert rows[0]["recordedEnding"] == 191
    plain = run_module(
        "lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES_UNLOADABLE, saved=SAVED
    )
    assert plain[0]["recordedEnding"] is None


def _actions_for(games: list[dict[str, Any]]) -> dict[str, Any]:
    return run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          document.appendChild(tbody);
          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };
          globalThis.fetch = async (path) => {
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };
          const shown = [];
          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {}, showReview: (r) => shown.push(r) });
          await new Promise((resolve) => setTimeout(resolve, 20));
          const cells = tbody.children[0].children;
          const actions = cells[cells.length - 1];
          const review = actions.children.find((b) => b.textContent === "Show thinking");
          if (review) review.listeners.click();
          return {
            labels: actions.children.map((b) => b.textContent),
            reviewTitle: review ? review.title || review.attrs.title || null : null,
            shown,
          };
        })()
        """,
        games=games,
        saved=SAVED,
    )


def test_an_unopenable_game_the_ledger_says_ended_still_offers_its_review() -> None:
    """The defect this pins: a game somebody played to the end became
    unreachable the day the engine's schema moved. The board is still
    undecodable and Open is still withheld - but `GET .../review` rebuilds
    the whole reading from the ledger, so hiding the door would be hiding a
    page that works."""
    result = _actions_for(GAMES_UNLOADABLE_ENDED)
    assert result["labels"] == ["End", "Show thinking"], result["labels"]
    assert result["shown"] == [{"gameId": "g1", "uid": "u1"}]
    assert "ledger" in (result["reviewTitle"] or "").lower(), (
        "the button must say what it will show - a reading rebuilt from the "
        f"ledger, not a final board - got {result['reviewTitle']!r}"
    )


def test_an_unopenable_game_with_no_recorded_ending_offers_no_review_button() -> None:
    """Nothing establishes that this game is finished: not the board (it
    cannot be read) and not the ledger. A review of a game still being played
    would hand one seat the other's plan, and the route refuses it - so there
    is no button here to press, rather than one that 409s."""
    result = _actions_for(GAMES_UNLOADABLE)
    assert result["labels"] == ["End"]
    assert result["shown"] == []


# --------------------------------------------------------------------------
# telling two games apart: a short game id, a relative timestamp, and
# newest-first ordering (see magic_cards_model_api.decks_api.
# list_lobby_games for the server half of the ordering fix)
# --------------------------------------------------------------------------


def test_short_game_id_is_the_first_eight_hex_chars() -> None:
    """The lobby row's disambiguator is the same prefix a human already sees
    in server logs and URLs - `game_id` is a bare `uuid4().hex`
    (`app.py`'s own docstring), so the first 8 chars is a short, stable slice
    of the real id, not a fabricated label."""
    assert (
        run_module("lobby_model.js", "m.shortGameId(id)", id="045634daabcffa11beef00112233445")
        == "045634da"
    )


def test_short_game_id_handles_a_missing_id() -> None:
    assert run_module("lobby_model.js", "m.shortGameId(id)", id=None) == "-"


def _iso_ms_before(now_ms: int, delta_ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp((now_ms - delta_ms) / 1000, tz=UTC).isoformat()


def test_relative_time_buckets() -> None:
    """Pure and deterministic: `nowMs` is passed explicitly rather than read
    from the wall clock, so this pins exact wording without being sensitive
    to when the test happens to run."""
    now = 1_800_000_000_000  # an arbitrary fixed instant, ms since epoch
    assert run_module("lobby_model.js", "m.relativeTime(iso, now)", iso=None, now=now) is None
    assert (
        run_module("lobby_model.js", "m.relativeTime(iso, now)", iso="not-a-date", now=now) is None
    )
    assert (
        run_module(
            "lobby_model.js",
            "m.relativeTime(iso, now)",
            iso=_iso_ms_before(now, 5_000),
            now=now,
        )
        == "just now"
    )
    assert (
        run_module(
            "lobby_model.js",
            "m.relativeTime(iso, now)",
            iso=_iso_ms_before(now, 3 * 60_000),
            now=now,
        )
        == "3m ago"
    )
    assert (
        run_module(
            "lobby_model.js",
            "m.relativeTime(iso, now)",
            iso=_iso_ms_before(now, 2 * 3_600_000),
            now=now,
        )
        == "2h ago"
    )
    assert (
        run_module(
            "lobby_model.js",
            "m.relativeTime(iso, now)",
            iso=_iso_ms_before(now, 5 * 86_400_000),
            now=now,
        )
        == "5d ago"
    )
    # Past the 30-day bucket, an absolute date rather than "40d ago" - by
    # that point "how long ago" is no longer the useful question.
    forty_days_iso = _iso_ms_before(now, 40 * 86_400_000)
    expected_date = forty_days_iso[:10]
    assert (
        run_module("lobby_model.js", "m.relativeTime(iso, now)", iso=forty_days_iso, now=now)
        == expected_date
    )
    # Clock skew (a timestamp that is technically in the future) must not
    # read as a negative duration - it reads the same as "just now".
    assert (
        run_module(
            "lobby_model.js",
            "m.relativeTime(iso, now)",
            iso=_iso_ms_before(now, -10_000),
            now=now,
        )
        == "just now"
    )


def test_row_timestamp_prefers_created_at_falls_back_to_ended_at() -> None:
    """The single "when" a row can show: `createdAt` for a live/listed row,
    `endedAt` for one only `mcm.games.v1` still remembers (the server no
    longer lists an archived game, so it never carries a `created_at`)."""
    assert (
        run_module(
            "lobby_model.js",
            "m.rowTimestamp(row)",
            row={"createdAt": "2026-09-01T00:00:00Z", "endedAt": "2026-09-05T00:00:00Z"},
        )
        == "2026-09-01T00:00:00Z"
    )
    assert (
        run_module(
            "lobby_model.js",
            "m.rowTimestamp(row)",
            row={"createdAt": None, "endedAt": "2026-09-05T00:00:00Z"},
        )
        == "2026-09-05T00:00:00Z"
    )
    assert (
        run_module(
            "lobby_model.js", "m.rowTimestamp(row)", row={"createdAt": None, "endedAt": None}
        )
        is None
    )


def test_ended_rows_sort_newest_ended_first() -> None:
    """Two browser-ended games this server no longer lists must still read
    newest-first, same as the live rows above them - `lobbyViewModel` sorts
    them by `endedAt` itself since the server has nothing left to sort."""
    saved = [
        {
            "gameId": "old",
            "uid": "u1",
            "aiUid": "u2",
            "opponent": "bot",
            "endedAt": "2026-09-01T00:00:00Z",
        },
        {
            "gameId": "new",
            "uid": "u3",
            "aiUid": "u4",
            "opponent": "bot",
            "endedAt": "2026-09-08T00:00:00Z",
        },
    ]
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, saved)", games=[], saved=saved)
    assert [r["gameId"] for r in rows] == ["new", "old"]


def test_lobby_row_carries_the_server_created_at() -> None:
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES, saved=SAVED)
    assert rows[0]["createdAt"] == "2026-09-08T12:00:00+00:00"


def test_the_games_table_shows_a_short_id_and_a_populated_started_cell() -> None:
    """The row-level regression this whole defect traces to: two games
    against the same opponent on the same decks used to render identically.
    A short, stable id and a populated timestamp cell are what tell them
    apart now."""
    games_with_id = [{**GAMES[0], "game_id": "045634daabcffa11beef00112233445"}]
    saved_for_id = [{"gameId": "045634daabcffa11beef00112233445", "uid": "u1", "aiUid": "u2"}]
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const table = document.createElement("table");
          table.setAttribute("id", "games-table");
          const tbody = document.createElement("tbody");
          tbody.setAttribute("id", "games-tbody");
          table.appendChild(tbody);
          document.appendChild(table);
          const storageData = { "mcm.games.v1": JSON.stringify(saved) };
          globalThis.localStorage = {
            getItem: (k) => (k in storageData ? storageData[k] : null),
            setItem: (k, v) => { storageData[k] = v; },
            removeItem: (k) => { delete storageData[k]; },
          };
          globalThis.fetch = async (path) => {
            if (path.startsWith("/api/decks")) {
              return { ok: true, status: 200, json: async () => ({ decks: [] }) };
            }
            return { ok: true, status: 200, json: async () => ({ games }) };
          };
          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {} });
          await new Promise((resolve) => setTimeout(resolve, 20));
          const cells = tbody.children[0].children;
          const gameCell = cells[1];
          const startedCell = cells[2];
          const stateCell = cells[cells.length - 2];
          return {
            rowText: cells.map((td) => td.textContent),
            gameTitle: gameCell.attrs.title || null,
            startedTitle: startedCell.attrs.title || null,
            stateClass: stateCell.className || null,
          };
        })()
        """,
        games=games_with_id,
        saved=saved_for_id,
    )
    assert result["rowText"][1] == "045634da"
    assert result["gameTitle"] == "045634daabcffa11beef00112233445"
    assert result["rowText"][2] != "-"
    assert result["startedTitle"] and "2026-09-08" in result["startedTitle"]
    assert "state-playing" in (result["stateClass"] or "")
