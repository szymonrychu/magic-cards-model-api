"""`ui/render/review.js` - the page a sceptic reads after the game.

Run against a fake DOM and a stubbed global `fetch`, the way
`tests/test_ui_ribbon.py` and `tests/test_ui_lobby.py` run their own render
modules: the real shipped module, under Node, no bundler and no library.

What is pinned here is the argument the page makes, not its styling:

- the prediction and what the opponent actually did are rendered *inside one
  element*, so no layout change can quietly separate the two halves of the
  claim;
- the ledger `seq` of the declaration is shown next to the `seq` range of the
  rows it is paired with, because "it was written first" is the whole point
  and a reader has to be able to check it;
- a live game renders nothing at all - the endpoint's 409 message is shown
  and no plan, prediction or reasoning reaches the page;
- the post-game reflection is last, and is labelled as written after the
  result was known.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node

# `review.js` builds its nodes through `dom.js`'s `h()` and draws boards with
# `cards.js`/`board_layout.js`, so this fake DOM is the `h()`-compatible one
# (`test_ui_ribbon.py`'s shape): a real `classList`, `dataset`, `childNodes`/
# `insertBefore`/`removeChild` for `patchChildren`, and a `textContent`
# getter that walks the subtree.
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this.listeners = {};
    this._text = "";
    this.classList = {
      set: new Set(),
      add: (c) => this.classList.set.add(c),
      remove: (c) => this.classList.set.delete(c),
      toggle: (c, on) => (on ? this.classList.set.add(c) : this.classList.set.delete(c)),
      contains: (c) => this.classList.set.has(c),
    };
  }
  set className(v) {
    this.classList.set = new Set(String(v).split(/\\s+/).filter(Boolean));
    this.attrs.class = v;
  }
  get className() { return [...this.classList.set].join(" "); }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  removeEventListener(type, fn) { if (this.listeners[type] === fn) delete this.listeners[type]; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (!this.children.length) return this._text;
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join(" ");
  }
  querySelector(sel) {
    const walk = (node) => {
      if (!node) return null;
      if (matches(node, sel)) return node;
      for (const c of node.children || []) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      if (matches(node, sel)) out.push(node);
      for (const c of node.children || []) walk(c);
    };
    walk(this);
    return out;
  }
}

function matches(node, sel) {
  if (!node || !node.attrs) return false;
  if (sel.startsWith("#")) return node.attrs.id === sel.slice(1);
  if (sel.startsWith(".")) return node.classList && node.classList.contains(sel.slice(1));
  return node.tagName === sel;
}

globalThis.document = new El("#document");
document.createElement = (t) => new El(t);
document.createTextNode = (t) => {
  const n = new El("#text");
  n.textContent = String(t);
  return n;
};

function section(id) {
  const el = document.createElement("section");
  el.setAttribute("id", id);
  document.appendChild(el);
  return el;
}

function findAll(node, cls) {
  const out = [];
  const walk = (n) => {
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children || []) walk(c);
  };
  walk(node);
  return out;
}

function click(el) {
  if (el && el.listeners && el.listeners.click) el.listeners.click({ target: el });
}

function mountInto() {
  section("lobby");
  const review = section("review");
  const head = document.createElement("div");
  head.setAttribute("id", "review-head");
  const body = document.createElement("div");
  body.setAttribute("id", "review-body");
  review.appendChild(head);
  review.appendChild(body);
  return { review, head, body };
}
"""


def snapshot() -> dict[str, Any]:
    return {
        "life": {"alice": 20, "claude": 17},
        "pools": {"alice": {}, "claude": {}},
        "battlefield": ["alice: Mountain (tapped)", "claude: Grizzly Bears [2/2]"],
        "hands": {"alice": ["Lightning Bolt"], "claude": ["Forest"]},
        "stack": [],
        "graveyards": {"alice": [], "claude": []},
        "exile": [],
        "combat": [],
    }


def entry(seq: int, action: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": seq,
        "ts": "2026-09-08T20:03:00+00:00",
        "action": action,
        "turn": 4,
        "step": "precombat_main",
        "phase": "precombat_main",
        "detail": {},
        "reasoning": None,
        "snapshot": snapshot(),
    }
    row.update(over)
    return row


#: One of the engine's own refusals, quoted exactly as `audit.py` recorded it.
REFUSAL = "claude cannot play a land: 1 of the 1 they may play this turn already used"

PREDICTION = {
    "expecting": "alice takes the two rather than trading her only blocker",
    "because": "her Bears are her clock, not her defence",
    "confidence": "medium",
}

OPPONENT_ROW = {
    "seq": 106,
    "ts": "2026-09-08T20:04:00+00:00",
    "turn": 4,
    "step": "declare_blockers",
    "phase": "combat",
    "action": "decide",
    "reasoning": None,
    "detail": {"player": "alice", "allowed": True, "card": "Grizzly Bears"},
    "player": "alice",
}

REVIEW = {
    "game_id": "g1",
    "players": ["alice", "claude"],
    "created_at": "2026-09-08T20:00:00+00:00",
    "ended_at": None,
    "is_over": True,
    "winner": "claude",
    "drawn": False,
    "ended_seq": 108,
    "declarations": [
        {
            "seq": 105,
            "ts": "2026-09-08T20:03:30+00:00",
            "turn": 4,
            "turns_taken": 4,
            "step": "precombat_main",
            "phase": "precombat_main",
            "player": "claude",
            "board_read": "she has a 2/2 and two open Mountains",
            "my_role": "the beatdown, and one turn ahead on board",
            "plan": "attack with the Bears and make her choose",
            "prediction": PREDICTION,
            "did": [],
            "opponent_did": [OPPONENT_ROW],
        }
    ],
    "entries": [
        entry(
            104,
            "cast",
            reasoning="a 2/2 now is worth more than the same 2/2 two turns later",
            detail={"player": "claude", "card": "Grizzly Bears", "allowed": True},
        ),
        entry(105, "plan_declared", detail={"player": "claude", **PREDICTION}),
        entry(106, "decide", detail={"player": "alice", "allowed": True}),
        entry(
            107,
            "play_land",
            reasoning="I wanted the fourth land",
            detail={
                "player": "claude",
                "card": "Forest",
                "allowed": False,
                "reason": REFUSAL,
            },
        ),
        entry(108, "game_over", detail={"winner": "claude"}),
    ],
}

CARD_ANSWER = {
    "requested": "Grizzly Bears",
    "name": "Grizzly Bears",
    "images": {"normal": "http://img/bears.png", "small": "http://img/bears-s.png"},
    "ok": True,
    "card": {"name": "Grizzly Bears", "type_line": "Creature - Bear", "oracle_text": ""},
}


def drive(body: str, **bindings: Any) -> Any:
    """Mount the review against the fake DOM and run `body` after it has
    settled, with `open()` already called."""
    return run_module("render/review.js", FAKE_DOM + body, **bindings)


FETCH = """
          globalThis.fetch = async (path) => {
            calls.push(path);
            if (path.includes("/review")) {
              if (status !== 200) {
                return { ok: false, status, json: async () => ({ detail: detailText }) };
              }
              return { ok: true, status: 200, json: async () => review };
            }
            return { ok: true, status: 200, json: async () => card };
          };
"""


def test_the_prediction_and_the_outcome_are_rendered_in_one_element() -> None:
    """The single most convincing element on the page, and the one a layout
    change must not be able to pull apart: what the agent said would happen
    and what the opponent actually did live in the same `.pairing`, with the
    `seq` of each so the ordering is checkable."""
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));

          const pairings = findAll(els.body, "pairing");
          return {
            calls,
            pairingCount: pairings.length,
            pairingText: pairings.length ? pairings[0].textContent : "",
            turnCount: findAll(els.body, "rev-turn").length,
            hidden: els.review.hidden,
            lobbyHidden: document.querySelector("#lobby").hidden,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert result["calls"][0] == "/api/games/g1/review?uid=u1"
    assert result["pairingCount"] == 1
    text = result["pairingText"]
    assert "alice takes the two rather than trading her only blocker" in text
    assert "her Bears are her clock, not her defence" in text
    assert "#105" in text, "the declaration's own seq has to be on the page"
    assert "#106" in text, "and the seq of the row it is paired with"
    assert "decide" in text
    assert result["hidden"] is False
    assert result["lobbyHidden"] is True


def test_each_action_is_a_frame_with_its_own_reasoning_and_its_own_board() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          const frames = findAll(els.body, "frame");
          return {
            frameCount: frames.length,
            bodyText: els.body.textContent,
            boards: findAll(els.body, "review-board").length,
            cards: findAll(els.body, "card").length,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    # four ledger rows, since the `plan_declared` row is the turn's header
    assert result["frameCount"] == 4
    assert "a 2/2 now is worth more than the same 2/2 two turns later" in result["bodyText"]
    assert result["boards"] >= 1, "a frame renders the board from its own snapshot"
    assert result["cards"] >= 1


def test_a_refused_action_shows_the_engine_s_own_words() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return {
            refusals: findAll(els.body, "refusal").map((el) => el.textContent),
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert result["refusals"] == [REFUSAL]


def test_a_live_game_renders_the_refusal_and_nothing_else() -> None:
    """The endpoint's 409 is the second gate (the lobby's own button is the
    first). What matters here is that nothing of the plan reaches the page
    when it fires."""
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 409;
          const detailText = liveDetail;
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return {
            bodyText: els.body.textContent,
            pairings: findAll(els.body, "pairing").length,
            frames: findAll(els.body, "frame").length,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        liveDetail=(
            "this game is still in progress - a review shows both seats' plans "
            "and reasoning, so it is only readable once the game is over or has "
            "been ended"
        ),
    )
    assert "still in progress" in result["bodyText"]
    assert result["pairings"] == 0
    assert result["frames"] == 0
    assert "alice takes the two" not in result["bodyText"]


def test_a_game_with_no_declarations_still_renders_and_says_so() -> None:
    plain = {**REVIEW, "declarations": [], "entries": [REVIEW["entries"][0]]}
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return {
            pairings: findAll(els.body, "pairing").length,
            frames: findAll(els.body, "frame").length,
            headText: els.head.textContent,
            notesInBody: findAll(els.body, "no-declaration").length,
          };
        })()
        """,
        review=plain,
        card=CARD_ANSWER,
    )
    assert result["pairings"] == 0
    assert result["frames"] == 1
    assert "no seat in this game declared a plan" in result["headText"].lower()
    assert result["notesInBody"] == 0, (
        "said once at the top, not repeated under every turn of a game that "
        "never had a declaration to make"
    )


def test_the_reflection_is_last_and_labelled_as_written_after_the_result() -> None:
    reflective = {
        **REVIEW,
        "reflection": {
            "text": "her removal was all sorcery speed and I could have committed harder",
            "player": "claude",
        },
    }
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          const kids = els.body.children;
          const last = kids[kids.length - 1];
          return {
            lastClass: last.className,
            lastText: last.textContent,
            count: findAll(els.body, "reflection").length,
          };
        })()
        """,
        review=reflective,
        card=CARD_ANSWER,
    )
    assert result["count"] == 1
    assert "reflection" in result["lastClass"]
    assert "after the result was known" in result["lastText"]
    assert "her removal was all sorcery speed" in result["lastText"]


def test_back_returns_to_the_lobby() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          let backs = 0;
          const api = m.mountReview(document, { onBack: () => { backs += 1; } });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          const back = document.querySelector("#review-back");
          click(back);
          return {
            backs,
            reviewHidden: els.review.hidden,
            lobbyHidden: document.querySelector("#lobby").hidden,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert result["backs"] == 1
    assert result["reviewHidden"] is True
    assert result["lobbyHidden"] is False


# ---------------------------------------------------------------------------
# a review rebuilt from the ledger
# ---------------------------------------------------------------------------

#: The engine's own refusal, as `GET .../review` hands it back in
#: `unloadable` for a game whose stored snapshot this build will not decode.
UNLOADABLE = (
    "Game snapshot was written at schema_version=3, but this build decodes "
    "schema_version=5. Refusing to decode a mismatched snapshot."
)

RECONSTRUCTED = {
    **REVIEW,
    "reconstructed": True,
    "unloadable": UNLOADABLE,
    "unestablished": [],
}


def test_a_reconstructed_review_says_so_and_quotes_the_engines_refusal() -> None:
    """The reading is the same reading - every frame on the page was recorded
    when it happened - but a reader must not be left thinking this was read
    off a final board, because it was not."""
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return {
            headText: els.head.textContent,
            notes: findAll(els.head, "review-reconstructed").length,
            pairings: findAll(els.body, "pairing").length,
          };
        })()
        """,
        review=RECONSTRUCTED,
        card=CARD_ANSWER,
    )
    assert result["notes"] == 1
    assert "schema_version=3" in result["headText"], (
        "the engine's own words, where the person reading the page can see them"
    )
    assert "claude won" in result["headText"], (
        "the outcome the ledger recorded is still shown - it is the head that "
        "gains a caveat, not the review that loses its result"
    )
    assert result["pairings"] == 1, "and the whole argument is still on the page"


# ---------------------------------------------------------------------------
# mtg-buddy's advice
# ---------------------------------------------------------------------------

ADVICE_DID_ROW = {
    "seq": 202,
    "ts": "2026-09-08T20:05:00+00:00",
    "turn": 4,
    "step": "precombat_main",
    "phase": "precombat_main",
    "action": "play_land",
    "reasoning": None,
    "detail": {"player": "claude", "card_id": "c1", "allowed": True},
    "player": "claude",
}

IGNORED_DID_ROW = {
    "seq": 203,
    "ts": "2026-09-08T20:05:30+00:00",
    "turn": 4,
    "step": "precombat_main",
    "phase": "precombat_main",
    "action": "pass_priority",
    "reasoning": None,
    "detail": {"player": "claude", "allowed": True},
    "player": "claude",
}

REFUSAL_REASON = "claude cannot play a land: 1 of the 1 they may play this turn already used"

REFUSED_ROW = {
    "seq": 204,
    "ts": "2026-09-08T20:05:45+00:00",
    "turn": 4,
    "step": "precombat_main",
    "phase": "precombat_main",
    "action": "play_land",
    "reasoning": None,
    "detail": {"player": "claude", "card_id": "c1", "allowed": False, "reason": REFUSAL_REASON},
    "player": "claude",
}


def advice_item(seq: int, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": seq,
        "ts": "2026-09-08T20:04:30+00:00",
        "turn": 4,
        "step": "precombat_main",
        "phase": "precombat_main",
        "player": "claude",
        "summary": "play the Forest",
        "reasoning": "a fourth land keeps the curve on track",
        "path": "/api/games/g1/actions/play-land",
        "body": {"card_id": "c1"},
        "based_on_seq": 199,
        "validated": True,
        "approved": False,
        "approved_seq": None,
        "did": None,
        "followed": False,
        "refused": [],
    }
    row.update(over)
    return row


def _advice_review(
    advice_item_row: dict[str, Any], *extra_entries: dict[str, Any]
) -> dict[str, Any]:
    return {
        **REVIEW,
        "advice": [advice_item_row],
        "entries": [
            *REVIEW["entries"],
            entry(200, "advice", detail={"player": "claude"}),
            *extra_entries,
        ],
    }


def test_advice_approved_and_followed_shows_the_summary_reasoning_and_a_frame() -> None:
    review = _advice_review(
        advice_item(200, approved=True, approved_seq=201, did=ADVICE_DID_ROW, followed=True),
    )
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          const advice = findAll(els.body, "advice");
          return {
            adviceCount: advice.length,
            adviceText: advice.length ? advice[0].textContent : "",
            tagText: findAll(els.body, "advice-tag").map((el) => el.textContent),
            emptyOutcomes: findAll(els.body, "advice-outcome-empty").length,
          };
        })()
        """,
        review=review,
        card=CARD_ANSWER,
    )
    assert result["adviceCount"] == 1
    assert "play the Forest" in result["adviceText"]
    assert "a fourth land keeps the curve on track" in result["adviceText"]
    assert "play_land" in result["adviceText"]
    assert result["tagText"] == ["approved at #201"]
    assert result["emptyOutcomes"] == 0


def test_advice_approved_but_refused_is_distinct_from_advice_ignored() -> None:
    """`did: null` renders the same empty-outcome line whether the approved
    attempt was refused or nothing else happened - the `advice-tag` (approved
    vs never approved) is what tells the two apart, exactly as `review.py`
    intends."""
    refused = _advice_review(
        advice_item(200, approved=True, approved_seq=201, did=None, followed=False)
    )
    ignored = _advice_review(
        advice_item(200, approved=False, approved_seq=None, did=None, followed=False)
    )

    def outcomes(review: dict[str, Any]) -> dict[str, Any]:
        return drive(
            """
            (async () => {
              const els = mountInto();
              const calls = [];
              const status = 200;
              const detailText = "";
              """
            + FETCH
            + """
              const api = m.mountReview(document, { onBack: () => {} });
              await api.open({ gameId: "g1", uid: "u1" });
              await new Promise((r) => setTimeout(r, 30));
              return {
                tagText: findAll(els.body, "advice-tag").map((el) => el.textContent),
                emptyText: findAll(els.body, "advice-outcome-empty").map((el) => el.textContent),
              };
            })()
            """,
            review=review,
            card=CARD_ANSWER,
        )

    refused_result = outcomes(refused)
    ignored_result = outcomes(ignored)
    assert refused_result["tagText"] == ["approved at #201"]
    assert ignored_result["tagText"] == ["never approved"]
    assert len(refused_result["emptyText"]) == 1
    assert len(ignored_result["emptyText"]) == 1
    assert refused_result["emptyText"] == ignored_result["emptyText"], (
        "the same honest line either way - only the tag says which outcome this is"
    )


def test_advice_approved_and_refused_shows_the_engines_own_reason() -> None:
    """The fourth outcome, visibly distinct from the ambiguous blank above:
    when `advice.refused` actually carries a row, the engine's own verbatim
    words render in its place rather than the generic empty-outcome line."""
    review = _advice_review(
        advice_item(
            200,
            approved=True,
            approved_seq=201,
            did=None,
            followed=False,
            refused=[REFUSED_ROW],
        ),
    )
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return {
            tagText: findAll(els.body, "advice-tag").map((el) => el.textContent),
            refusalText: findAll(els.body, "advice-refusal").map((el) => el.textContent),
            emptyOutcomes: findAll(els.body, "advice-outcome-empty").length,
          };
        })()
        """,
        review=review,
        card=CARD_ANSWER,
    )
    assert result["tagText"] == ["approved at #201"]
    assert len(result["refusalText"]) == 1
    assert REFUSAL_REASON in result["refusalText"][0]
    assert "play_land" in result["refusalText"][0]
    assert result["emptyOutcomes"] == 0, (
        "the refusal replaces the ambiguous blank - both must never render together"
    )


def test_advice_ignored_in_favour_of_something_else_shows_that_frame() -> None:
    review = _advice_review(
        advice_item(200, approved=False, approved_seq=None, did=IGNORED_DID_ROW, followed=False),
    )
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return {
            tagText: findAll(els.body, "advice-tag").map((el) => el.textContent),
            adviceText: findAll(els.body, "advice")[0].textContent,
            emptyOutcomes: findAll(els.body, "advice-outcome-empty").length,
          };
        })()
        """,
        review=review,
        card=CARD_ANSWER,
    )
    assert result["tagText"] == ["never approved"]
    assert "pass_priority" in result["adviceText"]
    assert result["emptyOutcomes"] == 0


def test_an_advice_approval_is_a_low_weight_frame_of_its_own() -> None:
    """The approval marker rides in the ledger's own frames (`review_model.js`'s
    `reviewTurns` never treats it as a turn header), drawn quietly - not
    folded into an unrelated run of `advance`/`pass_priority` rows."""
    review = {
        **REVIEW,
        "advice": [advice_item(200, approved=True, approved_seq=201)],
        "entries": [
            *REVIEW["entries"],
            entry(200, "advice", detail={"player": "claude"}),
            entry(201, "advice_approved", detail={"advice_seq": 200}),
        ],
    }
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          const approvals = findAll(els.body, "approval");
          return {
            count: approvals.length,
            text: approvals.length ? approvals[0].textContent : "",
          };
        })()
        """,
        review=review,
        card=CARD_ANSWER,
    )
    assert result["count"] == 1
    assert "approving advice #200" in result["text"]


def test_the_head_counts_the_pieces_of_advice() -> None:
    review = _advice_review(advice_item(200))
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return { headText: els.head.textContent };
        })()
        """,
        review=review,
        card=CARD_ANSWER,
    )
    assert "1 piece of advice" in result["headText"]


def test_a_game_with_no_advice_counts_zero_pieces_of_advice() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return { headText: els.head.textContent };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert "0 pieces of advice" in result["headText"]


def test_an_unestablished_outcome_is_never_rendered_as_the_game_is_over() -> None:
    """`is_over: null` means nothing established that this game finished.
    Saying "the game is over" there would be the page inventing the one fact
    the payload deliberately refused to invent."""
    unknown = {
        **RECONSTRUCTED,
        "is_over": None,
        "winner": None,
        "drawn": None,
        "ended_seq": None,
        "unestablished": ["is_over", "winner", "drawn"],
    }
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const calls = [];
          const status = 200;
          const detailText = "";
          """
        + FETCH
        + """
          const api = m.mountReview(document, { onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          await new Promise((r) => setTimeout(r, 30));
          return { headText: els.head.textContent };
        })()
        """,
        review=unknown,
        card=CARD_ANSWER,
    )
    assert "the game is over" not in result["headText"]
    assert "not recorded" in result["headText"]
