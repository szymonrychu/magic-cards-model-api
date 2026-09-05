"""`ui/render/lobby.js`'s `mountLobby` against a fake DOM and a stubbed global
`fetch` - no fixture server, no library, the same "run the real shipped
module under Node" approach every other `test_ui_*.py` file uses.

`mountLobby` had no tests of its own before this whole-branch review. The one
covered here is its most basic promise: `loadBundledDecks` fires a
`GET /api/ui/decks` and appends one `<option>` per name it gets back to
*both* seat pickers (`#you-bundled` and `#ai-bundled`) - the two-select
fan-out is the part worth pinning, since a one-select test would not catch a
regression that only wired one of them.
"""

from __future__ import annotations

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
        "step": "precombat_main",
        "winner": None,
        "drawn": None,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        "seats": {"you": "u1"},
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
            "step": "precombat_main",
            "over": False,
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
        "step": "precombat_main",
        "winner": None,
        "drawn": None,
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
        "step": "upkeep",
        "winner": None,
        "drawn": None,
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
    assert result["fetchCalls"] == ["/api/ui/decks"]
    expected = [
        {"value": "Modern Burn", "text": "Modern Burn"},
        {"value": "Modern Affinity", "text": "Modern Affinity"},
    ]
    assert result["you"] == expected
    assert result["ai"] == expected


def test_lobby_table_renders_a_row_and_end_deletes_the_game() -> None:
    """`mountLobby` reads `mcm.games.v1`, joins it against
    `GET /api/ui/games?uids=...` through `lobbyViewModel`, and renders one
    `<tr>` per row into `#games-tbody`. Clicking a row's End button calls
    `DELETE /api/ui/games/{id}?uid=<my seat uid>` and drops the row from both
    the table and `mcm.games.v1` - proven here by the saved list actually
    being empty afterward, not just the DELETE call having fired."""
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
            if (path.startsWith("/api/ui/decks")) {
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
    assert "GET /api/ui/games?uids=u1%2Cu2" in result["fetchCalls"]
    assert result["rowText"][0] == "bot"
    assert result["rowText"][3] == "playing"
    assert "DELETE /api/ui/games/g1?uid=u1" in result["fetchCalls"]
    assert result["rowCountAfter"] == 0
    assert result["savedAfter"] == []


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
            if (path.startsWith("/api/ui/decks")) {
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
