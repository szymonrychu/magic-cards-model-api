"""The mtg-buddy advisor panel: `ui/advice_model.js` (pure) and
`ui/render/advice.js` (`#advice-box`), both run as the real shipped modules
under Node.

The advisor is a seat that watches the human's own view and proposes a play
with its reasoning. A proposal is not a message on a side channel - it is a
row in this game's audit ledger (`action: "advice"`), so it arrives on the
very `GET /api/games/{id}/log?since=` read the page already makes every
1.2s, under that read's own staleness guard. Nothing here adds a third
concurrent read, and nothing here re-derives the board.

The model half exists so FRESHNESS is testable without a DOM, and it is
deliberately over-conservative: `isFresh` calls a proposal stale the moment
any log row that is not itself advice-related lands after the board the
agent read. A false "stale" costs the human one re-think; a false "fresh"
means approving a play against a board that has already moved, which is the
one failure this codebase guards against everywhere else (`poll()`'s
`mutations` guard, `boardIsNewest`).
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import add_card, card_id
from fastapi.testclient import TestClient
from ui_node import requires_node, run_module

from magic_cards_model_api import schemas
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

pytestmark = requires_node

# `tests/test_ui_ribbon.py`'s fake DOM - the one with REAL click dispatch (an
# event goes to the nearest inclusive ancestor still in the tree, and a
# detached node's handler never fires) and an un-parenting `removeChild`.
# That is the harness upgrade that made an eaten click testable at all, and
# the Approve button is exactly the control that must not be eaten: every
# renderer here rebuilds from scratch on every poll tick.
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
    this.classList = {
      set: new Set(),
      add: (c) => this.classList.set.add(c),
      remove: (c) => this.classList.set.delete(c),
      toggle: (c, on) => (on ? this.classList.set.add(c) : this.classList.set.delete(c)),
      contains: (c) => this.classList.set.has(c),
    };
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c);
    if (c.parentNode === this) c.parentNode = null;
    return c;
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  removeEventListener(type, fn) { if (this.listeners[type] === fn) delete this.listeners[type]; }
  contains(other) {
    if (other === this) return true;
    return this.children.some((c) => (c.contains ? c.contains(other) : false));
  }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get textContent() {
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join("");
  }
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

function findByText(node, tag, text) {
  const content = typeof node.textContent === "string" ? node.textContent : "";
  if (node.tagName === tag && content.includes(text)) return node;
  for (const c of node.children || []) {
    const found = findByText(c, tag, text);
    if (found) return found;
  }
  return null;
}

function attached(node) {
  let at = node;
  while (at.parentNode) {
    if (!at.parentNode.children.includes(at)) return false;
    at = at.parentNode;
  }
  return at === globalThis.document;
}

function click(el) {
  if (!el) return;
  let target = el;
  while (target && !attached(target)) target = target.parentNode || null;
  if (!target) return;
  for (let node = target; node; node = node.parentNode) {
    if (node.listeners && node.listeners.click) {
      node.listeners.click({ target, currentTarget: node });
    }
  }
}

globalThis.document = new El("#document");
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: s });
document.createDocumentFragment = () => new El("#fragment");

function boxed(id) {
  const el = document.createElement("div");
  el.setAttribute("id", id);
  document.appendChild(el);
  return el;
}

function makeStore(initial) {
  let state = initial;
  const listeners = [];
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      for (const fn of listeners) fn(state);
    },
    subscribe(fn) {
      listeners.push(fn);
      return () => {};
    },
  };
}

// One `action: "advice"` ledger row, as `audit.record` really writes it: the
// agent's own words in the row's TOP-LEVEL `reasoning` (the keyword argument
// every route passes, never a `detail` key), the server-derived `summary`,
// and the request it wants performed spelled out as `{method, path, body}`.
// `over` may still name `reasoning`; it is lifted out of `detail` here.
function advice(seq, over) {
  const detail = {
    player: "alice",
    method: "POST",
    path: "/api/games/g1/actions/cast",
    body: { card_id: "c1" },
    summary: "Cast Lightning Bolt targeting Grizzly Bears",
    reasoning: "Bears is their only blocker, and killing it now lets your two " +
      "attackers through for six before they untap.",
    based_on_seq: 10,
    validated: true,
    ...(over || {}),
  };
  const reasoning = detail.reasoning;
  delete detail.reasoning;
  return {
    seq, action: "advice", turn: 4, step: "precombat_main", phase: "precombat_main",
    reasoning, detail,
  };
}

function statusOf(box) {
  const found = [];
  const walk = (n) => {
    if (n.className && String(n.className).split(" ").includes("advice-status")) found.push(n);
    (n.children || []).forEach(walk);
  };
  walk(box);
  return found.map((n) => ({ text: n.textContent, className: n.className }));
}

function passEntry(seq, player) {
  return {
    seq, action: "pass_priority", turn: 4, step: "precombat_main",
    detail: { player, allowed: true },
  };
}

function approvedEntry(seq, adviceSeq) {
  return {
    seq, action: "advice_approved", turn: 4, step: "precombat_main",
    detail: { player: "alice", advice_seq: adviceSeq },
  };
}

const FRESH_LOG = { entries: [passEntry(10, "bob"), advice(11)], lastSeq: 11 };
const STALE_LOG = {
  entries: [passEntry(10, "bob"), advice(11), passEntry(12, "bob")],
  lastSeq: 12,
};
"""


#: The human-facing status per panel state (bd1). Distinct wording per
#: state, never the internal verification or staleness sentences.
READY = "Buddy is waiting for your call"
UNVERIFIED = "Buddy is unsure about this play"
BUSY = "Buddy is waiting for your play to finish"
STALE = "Buddy is thinking"


def model(expression: str, **bindings: object) -> object:
    return run_module("advice_model.js", FAKE_DOM + expression, **bindings)


def panel(expression: str, **bindings: object) -> object:
    return run_module("render/advice.js", FAKE_DOM + expression, **bindings)


# ------------------------------------------------------------ the model ----


def test_current_advice_is_the_newest_advice_row_addressed_to_this_player() -> None:
    """Two proposals in the ledger means the older one has been superseded -
    a newer advice row simply replaces it, so only the newest is ever the
    one on screen. The opponent's own advice row (their buddy pod advising
    them) is not this seat's business and must not surface here."""
    result = model(
        """
        (() => {
          const log = { entries: [
            advice(11, { summary: "older" }),
            advice(12, { player: "bob", summary: "the opponent's own advice" }),
            advice(13, { summary: "newest" }),
            passEntry(14, "bob"),
          ], lastSeq: 14 };
          const got = m.currentAdvice(log, "alice");
          return {
            summary: got.summary,
            seq: got.seq,
            turn: got.turn,
            step: got.step,
            method: got.method,
            path: got.path,
            body: got.body,
            basedOnSeq: got.based_on_seq,
            validated: got.validated,
            hasReasoning: got.reasoning.length > 0,
          };
        })()
        """
    )
    assert result["summary"] == "newest"
    assert result["seq"] == 13
    assert result["turn"] == 4
    assert result["step"] == "precombat_main"
    assert result["method"] == "POST"
    assert result["path"] == "/api/games/g1/actions/cast"
    assert result["body"] == {"card_id": "c1"}
    assert result["basedOnSeq"] == 10
    assert result["validated"] is True
    assert result["hasReasoning"] is True


def test_current_advice_is_null_when_nothing_has_been_proposed() -> None:
    assert model('m.currentAdvice({ entries: [passEntry(1, "bob")], lastSeq: 1 }, "alice")') is None
    assert model('m.currentAdvice({ entries: [], lastSeq: 0 }, "alice")') is None
    assert model('m.currentAdvice(null, "alice")') is None
    assert model("m.currentAdvice(FRESH_LOG, null)") is None


def test_a_proposal_stays_fresh_while_only_advice_rows_follow_the_board_it_read() -> None:
    """The advice row itself lands after `based_on_seq` (it is recorded once
    the agent has spoken), and so does the `advice_approved` row that pairs
    with an approval. Neither is the board moving, so neither stales
    anything - otherwise no proposal could ever be approved at all."""
    result = model(
        """
        (() => {
          const one = m.currentAdvice(FRESH_LOG, "alice");
          const withApproval = { entries: [
            passEntry(10, "bob"), advice(11), approvedEntry(12, 11), advice(13),
          ], lastSeq: 13 };
          const two = m.currentAdvice(withApproval, "alice");
          return {
            plain: m.isFresh(one, FRESH_LOG),
            alongsideApprovalAndNewerAdvice: m.isFresh(two, withApproval),
          };
        })()
        """
    )
    assert result["plain"] is True
    assert result["alongsideApprovalAndNewerAdvice"] is True


def test_any_other_row_after_the_board_it_read_stales_the_proposal() -> None:
    """Deliberately over-conservative: not only a real action, but the
    opponent's `plan_declared` and anybody's `report_problem` - rows that
    move nothing on the board - stale a proposal too. The rule is "the
    ledger grew in a way this agent has not seen", not "the board provably
    changed", because deciding the latter client-side means re-implementing
    the engine in the browser."""
    result = model(
        """
        (() => {
          const at = (seq, action) => ({
            seq, action, turn: 4, step: "precombat_main", detail: { player: "bob" },
          });
          const after = (row) => {
            const log = { entries: [passEntry(10, "bob"), advice(11), row], lastSeq: row.seq };
            return m.isFresh(m.currentAdvice(log, "alice"), log);
          };
          return {
            action: m.isFresh(m.currentAdvice(STALE_LOG, "alice"), STALE_LOG),
            plan: after(at(12, "plan_declared")),
            problem: after(at(12, "report_problem")),
            gameOver: after(at(12, "game_over")),
          };
        })()
        """
    )
    assert result == {"action": False, "plan": False, "problem": False, "gameOver": False}


def test_a_proposal_with_no_recorded_baseline_is_never_fresh() -> None:
    """`based_on_seq` is the whole basis of the freshness judgement. Without
    it there is nothing to compare the ledger against, and the conservative
    answer is the only honest one - an unknown baseline is not a fresh
    board."""
    result = model(
        """
        (() => {
          const log = { entries: [advice(11, { based_on_seq: null })], lastSeq: 11 };
          return {
            missing: m.isFresh(m.currentAdvice(log, "alice"), log),
            nothingProposed: m.isFresh(null, FRESH_LOG),
          };
        })()
        """
    )
    assert result == {"missing": False, "nothingProposed": False}


def test_the_staling_row_is_named_so_the_panel_can_say_which_one_it_was() -> None:
    """ "The board moved" is not much use without saying where to look. The
    seq reported is the FIRST non-advice row after the baseline - the one
    that actually invalidated the proposal, not the newest thing in the
    ledger."""
    result = model(
        """
        (() => {
          const log = { entries: [
            passEntry(10, "bob"), advice(11), passEntry(12, "bob"), passEntry(13, "alice"),
          ], lastSeq: 13 };
          return {
            staled: m.stalingSeq(m.currentAdvice(log, "alice"), log),
            fresh: m.stalingSeq(m.currentAdvice(FRESH_LOG, "alice"), FRESH_LOG),
          };
        })()
        """
    )
    assert result["staled"] == 12
    assert result["fresh"] is None


def test_the_proposed_path_is_sent_under_the_humans_own_uid_not_the_advisors() -> None:
    """The advisor seat is read-only server-side (`game_access.py`), so its
    own uid on the proposed path would be refused - and approving a play
    means the HUMAN made it. `send()` attaches this seat's uid to any path
    that carries none, so the proposal's own credential comes off first."""
    result = model(
        """
        ({
          advisors: m.withoutUid("/api/games/g1/actions/cast?uid=advisor-uid"),
          middle: m.withoutUid("/api/games/g1/actions/cast?uid=advisor-uid&dry_run=false"),
          trailing: m.withoutUid("/api/games/g1/actions/cast?dry_run=false&uid=advisor-uid"),
          none: m.withoutUid("/api/games/g1/actions/cast"),
        })
        """
    )
    assert result["advisors"] == "/api/games/g1/actions/cast"
    assert result["middle"] == "/api/games/g1/actions/cast?dry_run=false"
    assert result["trailing"] == "/api/games/g1/actions/cast?dry_run=false"
    assert result["none"] == "/api/games/g1/actions/cast"


# ----------------------------------------------------------- the panel ----


MOUNT = """
  boxed("advice-box");
  const calls = [];
  const store = makeStore(STORE);
  m.mountAdvice(document, store, { onApprove: (a) => calls.push(a) });
  store.set({});
  const box = document.querySelector("#advice-box");
  const approve = findByText(box, "button", "Approve");
"""


def test_a_fresh_proposal_shows_the_summary_the_rationale_and_an_enabled_approve() -> None:
    """The owner's own spec for this panel: "decision summary, rationale,
    approve button". Plus where it was made - the seq, turn and step - so
    the human can place it against the log."""
    result = panel(
        """
        (() => {
          const STORE = { log: FRESH_LOG, busy: 0, state: { you: "alice" } };
        """
        + MOUNT
        + """
          return {
            hidden: box.hidden,
            text: box.textContent,
            approveDisabled:
              approve === null
                ? null
                : Boolean(approve.disabled || approve.attrs.disabled !== undefined),
            lines: box.children.length,
            status: statusOf(box).map((x) => x.text),
          };
        })()
        """
    )
    assert result["hidden"] is False
    assert "Cast Lightning Bolt targeting Grizzly Bears" in result["text"]
    assert "Bears is their only blocker" in result["text"]
    assert "#11" in result["text"], "the proposal does not say which ledger entry it is"
    assert "turn 4" in result["text"]
    assert "precombat_main" in result["text"]
    assert result["approveDisabled"] is False
    assert "unchecked" not in result["text"]
    assert "board moved" not in result["text"]
    assert result["status"] == [READY], result["status"]


def test_nothing_is_rendered_at_all_when_there_is_no_proposal() -> None:
    """An empty panel, not an empty bordered box: `#side-panel > div` gives
    every child of that column a border and padding, so a panel with nothing
    to say has to take itself out of the column with `hidden` rather than
    leave a frame around nothing."""
    result = panel(
        """
        (() => {
          const STORE = {
            log: { entries: [passEntry(10, "bob")], lastSeq: 10 },
            busy: 0, state: { you: "alice" },
          };
        """
        + MOUNT
        + """
          return { hidden: box.hidden, children: box.children.length, text: box.textContent };
        })()
        """
    )
    assert result["hidden"] is True
    assert result["children"] == 0
    assert result["text"] == ""


def test_a_stale_proposal_keeps_its_words_dims_them_and_says_buddy_is_thinking() -> None:
    """Acceptance: keep the summary and the rationale readable (the thinking
    is still worth reading, and re-reading it is how the human decides
    whether it still applies), dim them, say the board moved and name the
    row that moved it, and DISABLE Approve rather than taking it away - a
    button that vanishes reads as a bug, and there is no Dismiss because a
    newer proposal simply replaces this one."""
    result = panel(
        """
        (() => {
          const STORE = { log: STALE_LOG, busy: 0, state: { you: "alice" } };
        """
        + MOUNT
        + """
          const classes = [];
          const walk = (n) => {
            if (n.className) classes.push(n.className);
            (n.children || []).forEach(walk);
          };
          walk(box);
          click(approve);
          return {
            text: box.textContent,
            classes,
            approveDisabled: Boolean(
              approve.disabled || approve.attrs.disabled !== undefined
            ),
            approveCalls: calls.length,
            dismiss: findByText(box, "button", "Dismiss") !== null,
            status: statusOf(box).map((x) => x.text),
          };
        })()
        """
    )
    assert "Cast Lightning Bolt targeting Grizzly Bears" in result["text"]
    assert "Bears is their only blocker" in result["text"]
    # The owner's report: internal staleness wording ("The board moved after
    # this was proposed - see entry #12") is not for the human panel. A short
    # status replaces it.
    assert "board moved" not in result["text"].lower()
    assert "see entry" not in result["text"]
    assert "#12" not in result["text"]
    assert result["status"] == [STALE], result["status"]
    assert result["approveDisabled"] is True
    assert result["approveCalls"] == 0, "a disabled Approve still reached the handler"
    assert result["dismiss"] is False
    assert any("stale" in c for c in result["classes"]), (
        "nothing on the stale proposal carries the class that dims it"
    )


def test_approve_is_disabled_while_one_of_this_seats_submissions_is_outstanding() -> None:
    """`busy` is the live half of `send()`'s `mutations` counter - non-zero
    for exactly as long as this seat is waiting on the server. Approving a
    play while another of the human's own plays is still in flight is
    submitting two actions against one board."""
    result = panel(
        """
        (() => {
          const STORE = { log: FRESH_LOG, busy: 1, state: { you: "alice" } };
        """
        + MOUNT
        + """
          click(approve);
          return {
            approveDisabled: Boolean(
              approve.disabled || approve.attrs.disabled !== undefined
            ),
            approveCalls: calls.length,
            stillShowsSummary: box.textContent.includes("Lightning Bolt"),
            status: statusOf(box).map((x) => x.text),
          };
        })()
        """
    )
    assert result["approveDisabled"] is True
    assert result["approveCalls"] == 0
    assert result["stillShowsSummary"] is True
    assert result["status"] == [BUSY], result["status"]


def test_a_shape_the_server_could_not_dry_run_shows_a_friendly_status() -> None:
    """`validated: false` means the server could not probe this request
    shape before recording it, so nobody has checked the engine would accept
    it. Saying "unchecked" out loud is the honest move - the alternative is a
    panel that looks equally confident about a play it has no evidence for."""
    result = panel(
        """
        (() => {
          const STORE = {
            log: { entries: [passEntry(10, "bob"), advice(11, { validated: false })], lastSeq: 11 },
            busy: 0, state: { you: "alice" },
          };
        """
        + MOUNT
        + """
          return {
            text: box.textContent,
            approveDisabled: Boolean(
              approve.disabled || approve.attrs.disabled !== undefined
            ),
            status: statusOf(box).map((x) => x.text),
          };
        })()
        """
    )
    # The owner's report: the internal verification sentence ("unchecked -
    # the server could not dry-run this shape...") is not for the human
    # panel. `validated` stays in the payload for agents and logs.
    assert "unchecked" not in result["text"]
    assert "dry-run" not in result["text"]
    assert result["status"] == [UNVERIFIED], result["status"]
    # Still a warning, not a refusal: the human may still approve it, and the
    # engine's own refusal is the backstop if it was wrong.
    assert result["approveDisabled"] is False


def test_approving_hands_back_the_seq_and_the_whole_request_to_perform() -> None:
    """The panel's entire job on the click: name the proposal (so the
    approval can be paired with it in the ledger) and hand over the request
    verbatim. It never assembles a request of its own."""
    result = panel(
        """
        (() => {
          const STORE = { log: FRESH_LOG, busy: 0, state: { you: "alice" } };
        """
        + MOUNT
        + """
          click(approve);
          return calls;
        })()
        """
    )
    assert len(result) == 1
    assert result[0]["seq"] == 11
    assert result[0]["method"] == "POST"
    assert result[0]["path"] == "/api/games/g1/actions/cast"
    assert result[0]["body"] == {"card_id": "c1"}


def test_only_one_proposal_is_ever_on_screen_and_a_newer_one_replaces_it() -> None:
    """A static popup, never a scrolling list (the owner's own words: "it
    mustn't be a scrolling log - more like a static popup"). The panel
    rebuilds from `clear()` on every render, so a second proposal replaces
    the first rather than joining it."""
    result = panel(
        """
        (() => {
          const STORE = { log: FRESH_LOG, busy: 0, state: { you: "alice" } };
        """
        + MOUNT
        + """
          const before = box.textContent;
          store.set({
            log: {
              entries: [
                ...FRESH_LOG.entries,
                advice(12, { summary: "Attack with both", based_on_seq: 11 }),
              ],
              lastSeq: 12,
            },
          });
          const after = box.textContent;
          return {
            before,
            after,
            approveButtons: (() => {
              let n = 0;
              const walk = (x) => {
                if (x.tagName === "button") n += 1;
                (x.children || []).forEach(walk);
              };
              walk(box);
              return n;
            })(),
          };
        })()
        """
    )
    assert "Lightning Bolt" in result["before"]
    assert "Attack with both" in result["after"]
    assert "Lightning Bolt" not in result["after"], "the superseded proposal is still on screen"
    assert result["approveButtons"] == 1


def test_the_panel_rerenders_when_busy_changes_even_though_the_log_did_not() -> None:
    """A render guard keyed on `log.lastSeq` alone would miss every other
    input this panel reads - the log, `busy`, and which seat you are - so
    all three are in its guard: an Approve that stayed enabled while a
    submission was outstanding is exactly the frozen control such a guard
    would leave behind."""
    result = panel(
        """
        (() => {
          const STORE = { log: FRESH_LOG, busy: 0, state: { you: "alice" } };
        """
        + MOUNT
        + """
          const disabledBefore = approve.disabled || approve.attrs.disabled !== undefined;
          const enabledBefore = !disabledBefore;
          store.set({ busy: 1 });
          const now = findByText(box, "button", "Approve");
          const enabledAfter = !(now.disabled || now.attrs.disabled !== undefined);
          store.set({ busy: 0, state: { you: "bob" } });
          return { enabledBefore, enabledAfter, otherSeatSees: box.textContent };
        })()
        """
    )
    assert result["enabledBefore"] is True
    assert result["enabledAfter"] is False
    assert result["otherSeatSees"] == "", "the other seat was shown a proposal addressed to alice"


# ----------------------------------------------------- bd1: rationale ----


def test_the_rationale_sent_through_the_real_route_is_what_the_panel_shows(
    client: TestClient, game: str
) -> None:
    """The owner's report, reproduced end to end: the buddy's rationale is in
    the ledger, and the panel still said "(no rationale was recorded)".
    `audit.record` lands `reasoning` top-level on the row; the panel read
    `detail.reasoning`."""
    store.register_advisors(game, {"buddy-uid": Advisor(player="alice")})
    add_card(client, game, "Mountain", "alice", "hand")
    land = card_id(client, game, "Mountain", "alice", "hand")
    said = "Land first: it turns on the bolt next turn."
    recorded = client.post(
        f"/api/games/{game}/advice",
        params={"uid": "buddy-uid"},
        json={
            "player": "alice",
            "method": "POST",
            "path": f"/api/games/{game}/actions/play-land",
            "body": {"player": "alice", "card_id": land},
            "reasoning": said,
            "validated": True,
        },
    )
    assert recorded.status_code == 200, recorded.text
    alice = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")
    read = client.get(f"/api/games/{game}/log", params={"uid": alice}).json()

    result = panel(
        """
        (() => {
          const STORE = {
            log: { entries: LOG.log, lastSeq: LOG.last_seq }, busy: 0, state: { you: "alice" },
          };
        """
        + MOUNT
        + """
          const why = [];
          const walk = (n) => {
            if (n.className === "advice-why") why.push(n.textContent);
            (n.children || []).forEach(walk);
          };
          walk(box);
          return { hidden: box.hidden, why };
        })()
        """,
        LOG=read,
    )

    assert result["hidden"] is False
    assert result["why"] == [said], result


def test_the_placeholder_appears_only_when_the_buddy_truly_sent_no_rationale() -> None:
    result = panel(
        """
        (() => {
          const STORE = {
            log: { entries: [passEntry(10, "bob"), advice(11, { reasoning: null })], lastSeq: 11 },
            busy: 0, state: { you: "alice" },
          };
        """
        + MOUNT
        + """
          return box.textContent;
        })()
        """
    )
    assert "(no rationale was recorded)" in result


def test_a_rationale_still_read_from_detail_is_shown_too() -> None:
    """Belt and braces with `review.py`, which reads top-level first and falls
    back to `detail.reasoning`: the two readers agree on the field."""
    result = model(
        """
        (() => {
          const row = advice(11);
          row.detail.reasoning = row.reasoning;
          row.reasoning = null;
          return m.currentAdvice([row], "alice").reasoning;
        })()
        """
    )
    assert result.startswith("Bears is their only blocker")


def test_the_panel_never_renders_an_unbounded_rationale() -> None:
    """Rows recorded before the cap (or by the admin key) can still be long;
    the panel clips to the same 240 characters the route enforces."""
    result = panel(
        """
        (() => {
          const STORE = {
            log: { entries: [passEntry(10, "bob"), advice(11, { reasoning: "y".repeat(900) })],
                   lastSeq: 11 },
            busy: 0, state: { you: "alice" },
          };
        """
        + MOUNT
        + """
          const why = [];
          const walk = (n) => {
            if (n.className === "advice-why") why.push(n.textContent);
            (n.children || []).forEach(walk);
          };
          walk(box);
          return why;
        })()
        """
    )
    assert len(result) == 1
    assert len(result[0]) <= 240, len(result[0])
    assert result[0].endswith("...")


def test_the_ui_cap_is_the_routes_cap() -> None:
    assert model("m.RATIONALE_MAX_CHARS") == schemas.ADVICE_REASONING_MAX_CHARS


def test_advice_status_names_each_state_distinctly() -> None:
    result = model(
        """
        (() => {
          const noBase = { entries: [passEntry(10, "bob"), advice(11, { based_on_seq: null })] };
          const unverified = { entries: [passEntry(10, "bob"), advice(11, { validated: false })] };
          const pick = (log, busy) => m.adviceStatus(m.currentAdvice(log, "alice"), log, busy);
          return {
            ready: pick(FRESH_LOG, 0),
            busy: pick(FRESH_LOG, 2),
            unverified: pick(unverified, 0),
            stale: pick(STALE_LOG, 0),
            staleBusy: pick(STALE_LOG, 1),
            noBase: pick(noBase, 0),
            none: m.adviceStatus(null, FRESH_LOG, 0),
          };
        })()
        """
    )
    assert result["ready"] == {"state": "ready", "text": READY}
    assert result["busy"] == {"state": "busy", "text": BUSY}
    assert result["unverified"] == {"state": "unverified", "text": UNVERIFIED}
    assert result["stale"] == {"state": "stale", "text": STALE}
    assert result["staleBusy"] == {"state": "stale", "text": STALE}
    assert result["noBase"] == {"state": "stale", "text": STALE}
    assert result["none"] is None
    assert len({READY, BUSY, UNVERIFIED, STALE}) == 4


# ------------------------------------------ the log poll feeds the panel ---

UI = Path(__file__).resolve().parents[1] / "ui"


def _side_panel_ids() -> list[str]:
    side = re.search(r'<aside id="side-panel">(.*?)</aside>', (UI / "index.html").read_text(), re.S)
    assert side, "could not find #side-panel in ui/index.html"
    return re.findall(r'<div id="([\w-]+)"', side.group(1))


def test_the_log_poll_still_feeds_the_advisor_panel_with_no_log_panel_on_the_page() -> None:
    """The game log panel is gone, but the log POLL is not: a proposal is a
    ledger row on `GET .../log?since=`, and `app.js`'s real `poll()` writing
    it into `app.log` is the only way it reaches `#advice-box`. Built from
    the real `#side-panel` children in `ui/index.html`, so there is no
    `#log-box` anywhere in this DOM."""
    ids = _side_panel_ids()
    assert "log-box" not in ids, ids
    result = panel(
        """
        (async () => {
          globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
          document.body = new El("body");
          const side = boxed("side-panel");
          for (const id of SIDE_IDS) {
            const el = document.createElement("div");
            el.setAttribute("id", id);
            side.appendChild(el);
          }
          const board = {
            you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
            active_player: "bob", priority_holder: "bob", battlefield: [],
            hands: { alice: [] }, stack: [], combat: { attacks: [] },
            winner: null, drawn: false, pregame: null,
          };
          const reads = [];
          globalThis.fetch = (path, opts) => {
            reads.push(path);
            if (path.includes("/log")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ log: [passEntry(10, "bob"), advice(11)], last_seq: 11 }),
              });
            }
            return Promise.resolve({ ok: true, json: async () => board });
          };
          const A = await import(APP_URL);
          m.mountAdvice(document, A.store, { onApprove: () => {} });
          A.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          const box = document.querySelector("#advice-box");
          const hiddenBefore = box.hidden;
          await A.poll();
          return {
            hiddenBefore,
            hidden: box.hidden,
            text: box.textContent,
            logRead: reads.some((path) => path.includes("log?since=0")),
            lastSeq: A.store.get().log.lastSeq,
            logBox: document.querySelector("#log-box"),
          };
        })()
        """,
        SIDE_IDS=ids,
        APP_URL=(UI / "app.js").as_uri(),
    )
    assert result["logBox"] is None
    assert result["hiddenBefore"] is True, "the panel showed a proposal before any log was read"
    assert result["logRead"] is True, "poll() no longer reads the log"
    assert result["lastSeq"] == 11
    assert result["hidden"] is False, "the proposal on the log read never reached the panel"
    assert "Cast Lightning Bolt targeting Grizzly Bears" in result["text"]
    assert "Approve" in result["text"]
