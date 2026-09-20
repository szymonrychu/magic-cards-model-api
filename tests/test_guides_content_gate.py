"""T38: `guides propose` gates the CONTENT of a proposal, not only its files.

`guides_propose` already refuses any FILE outside its allowlist. This is the
same discipline applied to the text: a proposal whose ADDED prose is about the
system rather than about playing the deck is refused with its own outcome,
`refused_content`, and the message points the seat at `report_problem`.

The gate is a heuristic that fails closed, which is the approved design; what
the tests here pin is the behaviour that follows from that decision - the
canonical violation is refused, real play prose still lands, only ADDED text
is judged, an undecidable proposal is refused rather than admitted, nothing is
ever edited or truncated on the way through, and `plays admit` is not gated.

Runs against the same tmp bare repo and fake GitHub REST server as
`test_guides_propose` (`guides_git_testkit`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from guides_git_testkit import (
    GAME,
    PLAN,
    FakeGitHub,
    branches,
    cli,
    clone,
    dump,
    git,
    interpose,
    isolate_git,
    main_sha,
    seed_guides,
)
from playscript_testkit import FETCH_SURVEIL, SLUG

from magic_cards_model_api.playscripts import content_gate
from magic_cards_model_api.playscripts.gitwork import SUCCESS

NOTES = f"decks/{SLUG}/NOTES.md"
BRANCH = f"seat/{GAME}-{SLUG}-seat-guides"

#: PR 132's actual content, and the worked example T37 put in both write-back
#: prompts: every word of it is true and it is still a report, because the
#: behaviour is the api's and the remedy is a tool argument.
CANONICAL_VIOLATION = (
    "Answering the fetchland's search decision released priority and cost me "
    "the whole main phase; the fix is `hold_priority=true` on `decide`.\n"
)

#: Real play prose, in the register a deck's NOTES.md is actually written in.
PLAY_PROSE = (
    "Against the frog deck, hold Lightning Bolt for the second threat rather "
    "than the first: the first one trades with a blocker and the second one "
    "is the one that closes the race.\n"
)


@pytest.fixture
def github(tmp_path, monkeypatch):
    fake = FakeGitHub()
    isolate_git(tmp_path, monkeypatch, fake)
    yield fake
    fake.close()


@pytest.fixture
def render_log(tmp_path, monkeypatch):
    log = tmp_path / "render.log"
    monkeypatch.setenv("MTG_TEST_RENDER_LOG", str(log))
    return log


@pytest.fixture
def bare(tmp_path, github):
    return seed_guides(tmp_path)


@pytest.fixture
def root(tmp_path, bare):
    return clone(bare, tmp_path / "guides")


def propose(capsys, root: Path):
    return cli(capsys, "guides", "propose", "--deck", SLUG, "--game-id", GAME, "--root", str(root))


def admit(capsys, root: Path):
    return cli(capsys, "plays", "admit", "--deck", SLUG, "--game-id", GAME, "--root", str(root))


def append_notes(root: Path, text: str) -> None:
    notes = root / NOTES
    notes.write_text(notes.read_text(encoding="utf-8") + text, encoding="utf-8")


def edit_plan(root: Path, change) -> None:
    doc = json.loads((root / PLAN).read_text(encoding="utf-8"))
    change(doc)
    (root / PLAN).write_text(dump(doc), encoding="utf-8")


def rendered_log(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# -- the outcome is its own, and it is not a success ----------


def test_the_content_refusal_is_a_distinct_outcome_and_never_a_success():
    assert content_gate.CONTENT_REFUSED != "refused"
    assert content_gate.CONTENT_REFUSED not in SUCCESS


# -- (a) the canonical violation is refused, and the message routes it ----------


def test_the_canonical_system_problem_is_refused_and_names_the_report_tool(
    capsys, root, bare, github, render_log
):
    append_notes(root, CANONICAL_VIOLATION)

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "report_problem" in result["message"]
    # It says WHY this text was judged a system problem, quoting what it read.
    assert "hold_priority" in result["message"]
    # Nothing was rendered, branched, pushed or opened.
    assert rendered_log(render_log) == []
    assert branches(bare) == {"main"}
    assert github.pulls() == []


def test_the_content_refusal_is_told_apart_from_the_file_path_refusal(
    capsys, root, bare, github, render_log
):
    (root / f"decks/{SLUG}/decklist.txt").write_text("4 Ragavan\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["result"] != content_gate.CONTENT_REFUSED


# -- (d)/(f) it refuses the whole proposal, before anything runs, and edits nothing ----------


def test_the_refusal_runs_before_any_commit_or_push_and_leaves_the_text_alone(
    capsys, monkeypatch, root, bare, github, render_log
):
    calls = interpose(monkeypatch)
    before = main_sha(bare)
    append_notes(root, PLAY_PROSE + CANONICAL_VIOLATION)
    written = (root / NOTES).read_text(encoding="utf-8")

    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result

    # The seat's file is byte-for-byte what the seat wrote: no edit, no
    # truncation, no filtering of the legitimate half.
    assert (root / NOTES).read_text(encoding="utf-8") == written
    assert PLAY_PROSE in written and CANONICAL_VIOLATION in written
    # Nothing was committed, branched or pushed.
    argv = [" ".join(c["argv"]) for c in calls]
    assert not [a for a in argv if " commit" in a or " push" in a or " switch -q -c" in a]
    assert main_sha(bare) == before
    assert branches(bare) == {"main"}
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"


# -- (b) fail closed ----------


def test_a_proposal_the_gate_cannot_read_is_refused_rather_than_admitted(
    capsys, root, bare, github, render_log
):
    (root / NOTES).write_bytes(b"# notes\n- \xff\xfe not text\n")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "could not decide" in result["message"].lower()
    assert branches(bare) == {"main"}
    assert github.pulls() == []


def test_a_plan_note_shape_the_gate_cannot_read_is_refused_rather_than_admitted(
    capsys, root, bare, github, render_log
):
    edit_plan(root, lambda doc: doc["matchups"][0].__setitem__("notes", "not a list"))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "could not decide" in result["message"].lower()
    assert branches(bare) == {"main"}


# -- (b) the happy path still works: the gate does not break what it guards ----------


def test_ordinary_play_prose_still_becomes_a_pull_request(capsys, root, bare, github, render_log):
    append_notes(root, PLAY_PROSE)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "awaiting_owner_review"), result
    assert BRANCH in branches(bare)


def test_a_plan_play_plan_and_notes_edit_still_becomes_a_pull_request(
    capsys, root, bare, github, render_log
):
    def change(doc):
        doc["matchups"][0]["play_plan"]["our_tactic"] = "go under the frog"
        doc["matchups"][0]["notes"].append("Hold Bolt for the second frog.")

    edit_plan(root, change)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "awaiting_owner_review"), result


# -- the plan's own editable fields are judged too ----------


def test_a_system_problem_in_a_matchup_note_is_refused(capsys, root, bare, github, render_log):
    edit_plan(
        root,
        lambda doc: doc["matchups"][0]["notes"].append(
            "The api returned a 422 when I sent the cast, so board in more removal."
        ),
    )
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "report_problem" in result["message"]


def test_a_system_problem_in_a_play_plan_value_is_refused(capsys, root, bare, github, render_log):
    edit_plan(
        root,
        lambda doc: doc["matchups"][0]["play_plan"].__setitem__(
            "our_tactic", "race them, but the MCP tool timed out on every attack"
        ),
    )
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result


# -- (e) ADDED text only ----------


def test_pre_existing_system_text_never_refuses_a_clean_edit(
    capsys, tmp_path, bare, github, render_log
):
    """A deck whose NOTES.md ALREADY trips the gate must stay editable -
    otherwise T40's clean-up could never be proposed."""
    dirty = clone(bare, tmp_path / "dirty-seed")
    (dirty / NOTES).write_text("# notes\n" + CANONICAL_VIOLATION, encoding="utf-8")
    git(dirty, "commit", "-q", "-am", "pre-existing violation")
    git(dirty, "push", "-q", "origin", "HEAD:main")

    root = clone(bare, tmp_path / "guides-2")
    append_notes(root, PLAY_PROSE)
    rc, result, _, _ = cli(
        capsys, "guides", "propose", "--deck", SLUG, "--game-id", GAME, "--root", str(root)
    )
    assert (rc, result["result"]) == (0, "awaiting_owner_review"), result


def test_pre_existing_system_text_in_a_matchup_note_never_refuses_a_clean_edit(
    capsys, tmp_path, bare, github, render_log
):
    dirty = clone(bare, tmp_path / "dirty-seed")
    doc = json.loads((dirty / PLAN).read_text(encoding="utf-8"))
    doc["matchups"][0]["notes"].append("The api 500s on the second fetch.")
    (dirty / PLAN).write_text(dump(doc), encoding="utf-8")
    git(dirty, "commit", "-q", "-am", "pre-existing violation")
    git(dirty, "push", "-q", "origin", "HEAD:main")

    root = clone(bare, tmp_path / "guides-2")
    edit_plan(root, lambda d: d["matchups"][0]["notes"].append("Bolt the frog on sight."))
    rc, result, _, _ = cli(
        capsys, "guides", "propose", "--deck", SLUG, "--game-id", GAME, "--root", str(root)
    )
    assert (rc, result["result"]) == (0, "awaiting_owner_review"), result


# -- (g) the gate is NOT applied to `plays admit` ----------


def test_plays_admit_is_not_content_gated(capsys, root, bare, github):
    """Every play `intent` in the DECKS corpus carries a "Limits:" clause about
    the DSL's own vocabulary gaps, in exactly the register this gate refuses."""
    play = copy.deepcopy(FETCH_SURVEIL) | {
        "id": "fetch-late",
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
        "intent": (
            "Fetch on their upkeep. Limits: the DSL has no `payable_any` tier for "
            "a life payment, so this cannot say --prefer the untapped land; "
            "`hold_priority=true` is required on the `decide` that answers the search."
        ),
    }
    (root / f"decks/{SLUG}/plays/fetch-late.json").write_text(dump(play), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result


# -- the gate itself: what it reads and what it says ----------


def test_added_lines_reports_only_what_is_new():
    before = "# notes\n- keep this\n"
    after = "# notes\n- keep this\n- brand new\n"
    assert content_gate.added_lines(before, after) == ["- brand new"]


def test_added_lines_treats_a_file_absent_from_head_as_all_new():
    assert content_gate.added_lines(None, "a\nb\n") == ["a", "b"]


def test_added_plan_passages_reports_only_the_new_strings():
    before = {"matchups": [{"play_plan": {"our_tactic": "race"}, "notes": ["old"]}]}
    after = {"matchups": [{"play_plan": {"our_tactic": "grind"}, "notes": ["old", "new"]}]}
    assert sorted(content_gate.added_plan_passages(before, after)) == ["grind", "new"]


def test_added_plan_passages_refuses_to_guess_at_a_shape_it_cannot_read():
    before = {"matchups": [{"notes": ["old"]}]}
    after = {"matchups": [{"notes": "old and new"}]}
    with pytest.raises(content_gate.Undecidable):
        content_gate.added_plan_passages(before, after)


SYSTEM_PASSAGES = [
    "The api returned a 422 for the cast.",
    "The MCP tool timed out.",
    "That is an engine bug, not a misplay.",
    "There is a rules gap around copying a token.",
    "Pass `hold_priority=true` on the decide.",
    "Run it with --deck instead.",
    "The fix is in `src/magic_cards_model_api/app.py`.",
    "The pod restarted mid-game.",
    "It raised an exception and the turn was lost.",
    "The endpoint answered with a stack trace.",
    "Use `mcp-seat guides propose` afterwards.",
]


@pytest.mark.parametrize("passage", SYSTEM_PASSAGES)
def test_system_prose_is_judged_a_system_problem(passage):
    assert content_gate.system_problem_reasons([passage])


PLAY_PASSAGES = [
    "Bolt the frog on sight.",
    "Board in all three copies of Rest in Peace against Living End.",
    "Hold up the fetch until their end step so the information is free.",
    "With the exception of the mirror, this deck wants to be the beatdown.",
    "The card-draw engine is Ragavan plus the two cantrips.",
    "Birthing Pod decks go wide, so keep the sweeper.",
    "Our tactic is to race them and deploy the threats in the latest window.",
    "Their stack of triggers resolves before ours, so hold the removal.",
]


@pytest.mark.parametrize("passage", PLAY_PASSAGES)
def test_play_prose_is_not_judged_a_system_problem(passage):
    assert content_gate.system_problem_reasons([passage]) == []


def test_the_refusal_message_names_the_destination_and_offers_the_way_out():
    message = content_gate.refusal_message(
        content_gate.system_problem_reasons(["The api returned a 422 for the cast."])
    )
    assert "report_problem" in message
    # It quotes what it read and why, so a human can reword rather than argue.
    assert "422" in message or "api" in message
    assert "reword" in message.lower()


# -- (c) the module is honest about what it is ----------


def test_the_module_docstring_states_the_ceiling_plainly():
    doc = (content_gate.__doc__ or "").lower()
    assert "heuristic" in doc
    assert "not a proof" in doc
    assert "false negative" in doc and "false positive" in doc
    assert "reword" in doc
    # (g)'s exclusion is stated with its reason, in the module that owns it.
    assert "plays admit" in doc


def test_the_gate_is_stdlib_only_so_it_runs_in_pod():
    source = Path(content_gate.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]
    assert imports
    assert all(
        line.split()[1].split(".")[0] in {"difflib", "re", "collections", "typing"}
        for line in imports
    ), imports
