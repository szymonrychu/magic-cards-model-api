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

T39 adds the half T38's own tests could not reach, below the divider near the
middle of this file: the same six clauses re-asked with REAL corpus text -
`tests/fixtures/notes/` for the guide side and the vendored
`tests/fixtures/guides/decks/*/plays/` for the play side - so the gate is
measured against prose a human accepted rather than against prose written to
pass it, plus the precedence cases (the file-path allowlist answers first,
`plays admit` is never asked) and the fail-closed shapes reachable only from
a plan that was ALREADY the wrong shape when this proposal started.

Runs against the same tmp bare repo as `test_guides_propose`
(`guides_git_testkit`).
"""

from __future__ import annotations

import copy
import difflib
import json
from pathlib import Path

import pytest
from guides_git_testkit import (
    GAME,
    GAMEPLAN,
    OLD_PLAN,
    PLAN,
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

#: PR 132's actual content, and the worked example T37 put in both write-back
#: prompts: every word of it is true and it is still a report, because the
#: behaviour is the api's and the remedy is a tool argument.
CANONICAL_VIOLATION = (
    "Answering the fetchland's search decision released priority and cost me "
    "the whole main phase; the fix is `hold_priority=true` on `decide`.\n"
)

#: A system problem written as an ordinary sentence, for the places that need
#: one inside a JSON value rather than on its own line.
SYSTEM_NOTE = "The api returned a 422 for the cast and the pod restarted."

#: Real play prose, in the register a deck's NOTES.md is actually written in.
PLAY_PROSE = (
    "Against the frog deck, hold Lightning Bolt for the second threat rather "
    "than the first: the first one trades with a blocker and the second one "
    "is the one that closes the race.\n"
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    return isolate_git(tmp_path, monkeypatch)


@pytest.fixture
def render_log(tmp_path, monkeypatch):
    log = tmp_path / "render.log"
    monkeypatch.setenv("MTG_TEST_RENDER_LOG", str(log))
    return log


@pytest.fixture
def bare(tmp_path, isolated):
    return seed_guides(tmp_path)


@pytest.fixture
def root(tmp_path, bare):
    return clone(bare, tmp_path / "guides")


def propose(capsys, root: Path):
    return cli(capsys, "guides", "propose", "--deck", SLUG, "--game-id", GAME, "--root", str(root))


def landed(bare: Path) -> bool:
    """The proposal is main's newest commit."""
    return git(bare, "log", "-1", "--format=%s", "main") == f"Guides: {SLUG} after game {GAME}"


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
    capsys, root, bare, isolated, render_log
):
    append_notes(root, CANONICAL_VIOLATION)

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "report_problem" in result["message"]
    # It says WHY this text was judged a system problem, quoting what it read.
    assert "hold_priority" in result["message"]
    # Nothing was rendered, committed or pushed.
    assert rendered_log(render_log) == []
    assert branches(bare) == {"main"}


def test_the_content_refusal_is_told_apart_from_the_file_path_refusal(
    capsys, root, bare, isolated, render_log
):
    (root / f"decks/{SLUG}/decklist.txt").write_text("4 Ragavan\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["result"] != content_gate.CONTENT_REFUSED


# -- (d)/(f) it refuses the whole proposal, before anything runs, and edits nothing ----------


def test_the_refusal_runs_before_any_commit_or_push_and_leaves_the_text_alone(
    capsys, monkeypatch, root, bare, isolated, render_log
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
    # Nothing was committed or pushed.
    argv = [" ".join(c["argv"]) for c in calls]
    assert not [a for a in argv if " commit" in a or " push" in a]
    assert main_sha(bare) == before
    assert branches(bare) == {"main"}
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"


# -- (b) fail closed ----------


def test_a_proposal_the_gate_cannot_read_is_refused_rather_than_admitted(
    capsys, root, bare, isolated, render_log
):
    (root / NOTES).write_bytes(b"# notes\n- \xff\xfe not text\n")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "could not decide" in result["message"].lower()
    assert branches(bare) == {"main"}


def test_a_plan_note_shape_the_gate_cannot_read_is_refused_rather_than_admitted(
    capsys, root, bare, isolated, render_log
):
    edit_plan(root, lambda doc: doc["matchups"][0].__setitem__("notes", "not a list"))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "could not decide" in result["message"].lower()
    assert branches(bare) == {"main"}


# -- (b) the happy path still works: the gate does not break what it guards ----------


def test_ordinary_play_prose_still_lands_on_main(capsys, root, bare, isolated, render_log):
    append_notes(root, PLAY_PROSE)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert landed(bare)


def test_a_plan_play_plan_and_notes_edit_still_lands_on_main(
    capsys, root, bare, isolated, render_log
):
    def change(doc):
        doc["matchups"][0]["play_plan"]["our_tactic"] = "go under the frog"
        doc["matchups"][0]["notes"].append("Hold Bolt for the second frog.")

    edit_plan(root, change)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result


# -- the plan's own editable fields are judged too ----------


def test_a_system_problem_in_a_matchup_note_is_refused(capsys, root, bare, isolated, render_log):
    edit_plan(
        root,
        lambda doc: doc["matchups"][0]["notes"].append(
            "The api returned a 422 when I sent the cast, so board in more removal."
        ),
    )
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "report_problem" in result["message"]


def test_a_system_problem_in_a_play_plan_value_is_refused(capsys, root, bare, isolated, render_log):
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
    capsys, tmp_path, bare, isolated, render_log
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
    assert (rc, result["result"]) == (0, "admitted"), result


def test_pre_existing_system_text_in_a_matchup_note_never_refuses_a_clean_edit(
    capsys, tmp_path, bare, isolated, render_log
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
    assert (rc, result["result"]) == (0, "admitted"), result


# -- (g) the gate is NOT applied to `plays admit` ----------


def test_plays_admit_is_not_content_gated(capsys, root, bare, isolated):
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


def test_added_plan_passages_judges_an_appended_rows_name_too():
    row = {"name": "the api returned a 422", "origin": "seat", "play_plan": {"keep": "k"}}
    before = {"matchups": [{"name": "dimir frog", "play_plan": {}, "notes": []}]}
    after = {"matchups": [*before["matchups"], {**row, "notes": ["n"]}]}
    assert sorted(content_gate.added_plan_passages(before, after)) == sorted(
        ["the api returned a 422", "k", "n"]
    )
    assert content_gate.system_problem_reasons(["the api returned a 422"])


def test_an_existing_rows_name_is_never_new_text():
    before = {"matchups": [{"name": "dimir frog", "notes": []}]}
    after = {"matchups": [{"name": "dimir frog", "notes": ["hold Bolt"]}]}
    assert content_gate.added_plan_passages(before, after) == ["hold Bolt"]


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


# ======================================================================
# T39: the cases T38's own tests do not reach.
#
# T38 pinned the gate against prose written for the test. That proves the
# test author and the gate author agree with each other, which is exactly the
# failure mode that ships a gate looking green and then refuses real work.
# Everything below is fed REAL corpus text instead - `tests/fixtures/notes/`
# for the guide side, the vendored `tests/fixtures/guides/decks/*/plays/` for
# the play side - plus the negative cases that decide whether a seat routes
# around this gate: that it judges only ADDED text even when the committed
# text is a real accepted section that trips it, that it fails closed on a
# shape it cannot read, that `plays admit` is genuinely outside it, and that
# the file-path allowlist still gets its answer in first.
# ======================================================================

NOTES_FIXTURES = Path(__file__).parent / "fixtures" / "notes"

#: Real, human-accepted play prose. See that directory's README for the pin
#: and the line ranges; each is a byte-exact cut, never retyped.
REAL_PLAY_PROSE = (
    "orzhov-martyr-clarion-conqueror",
    "eldrazi-tron-karn-coating-lock",
    "instant-reanimator-same-turn-rule",
)

#: A real, human-accepted NOTES.md section that IS about the system. It is
#: already committed in the corpus, which is the whole case for judging only
#: added text: judging the committed text would freeze that file for ever.
REAL_SYSTEM_PROSE = "dimir-frog-what-the-engine-will-not-measure"

#: Real corpus play intents, quoted through the vendored slice rather than
#: copied here, so a corpus refresh moves the fixture with it.
CORPUS_PLAYS = Path(__file__).parent / "fixtures" / "guides" / "decks"
#: Carries the "Limits:" clause the exclusion exists for - "the vocabulary
#: cannot join a card's mana value to the mana actually available".
LIMITS_PLAY = "boros-aggro/plays/fetch-untapped-shock.json"
#: Names the api, the sibling engine and two commit SHAs, in a play the
#: corpus ships: this one the gate really would refuse.
SYSTEM_PLAY = "prowess/plays/open-land-unlocks.json"


def notes_fixture(name: str) -> str:
    return (NOTES_FIXTURES / f"{name}.md").read_text(encoding="utf-8")


def corpus_intent(relative: str) -> str:
    return json.loads((CORPUS_PLAYS / relative).read_text(encoding="utf-8"))["intent"]


def passages(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def opcode_tags(before: str, after: str) -> list[str]:
    """The difflib opcodes `added_lines` walks for this pair.

    Asserted in the diff-shape tests below, because a test that says which
    opcode it exercises and does not produce it is a test of a second copy of
    a shape already covered. A fixture refresh that changes the shape has to
    come back here and say so."""
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    return [tag for tag, *_ in matcher.get_opcodes()]


def seed_notes(root: Path, text: str) -> None:
    """Put `text` into NOTES.md and land it on origin main, so it is text the
    gate must treat as PRE-EXISTING rather than as this proposal's."""
    (root / NOTES).write_text(text, encoding="utf-8")
    git(root, "commit", "-q", "-am", "notes as already accepted")
    git(root, "push", "-q", "origin", "HEAD:main")


# -- the fixtures are the two kinds they claim to be ----------


def test_the_vendored_notes_passages_are_the_two_kinds_they_claim_to_be():
    """If a corpus refresh made the system-side passage clean, every ADDED-text
    test below would quietly become a test of nothing. Fail here instead."""
    for name in REAL_PLAY_PROSE:
        assert content_gate.system_problem_reasons(passages(notes_fixture(name))) == [], name
    assert content_gate.system_problem_reasons(passages(notes_fixture(REAL_SYSTEM_PROSE)))


# -- (b) real accepted play prose still passes ----------


@pytest.mark.parametrize("name", REAL_PLAY_PROSE)
def test_a_real_accepted_notes_passage_is_not_judged_a_system_problem(name):
    """Not prose written to pass: prose a human already accepted into a deck's
    NOTES.md, naming a lifegain engine, quoting oracle text in backticks over a
    line break, and saying what "cannot be activated"."""
    assert content_gate.system_problem_reasons(passages(notes_fixture(name))) == []


@pytest.mark.parametrize("name", REAL_PLAY_PROSE)
def test_a_real_accepted_notes_passage_still_lands_on_main(
    capsys, root, bare, isolated, render_log, name
):
    append_notes(root, "\n" + notes_fixture(name))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert landed(bare)


# -- (c) only ADDED text is judged, over real committed system text ----------


def test_a_real_accepted_system_section_already_committed_still_takes_a_clean_addition(
    capsys, root, bare, isolated, render_log
):
    """`dimir-frog/NOTES.md` really does carry a section about the evidence
    harness. A gate that re-judged committed text would freeze that file."""
    seed_notes(root, "# notes\n\n" + notes_fixture(REAL_SYSTEM_PROSE))
    append_notes(root, "\n" + notes_fixture(REAL_PLAY_PROSE[0]))

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (0, "admitted"), result
    assert landed(bare)


def test_a_clean_passage_inserted_between_pre_existing_system_lines_is_still_only_added_text(
    capsys, root, bare, isolated, render_log
):
    """Appending is the easy shape: one trailing opcode with nothing after it
    to get wrong. Inserting into the MIDDLE puts pre-existing system lines on
    BOTH sides of the added block, so a gate that widened an opcode by a line,
    or judged from the first change to the end of the file, refuses text this
    proposal never touched.

    The opcode really is `insert` and not `replace`: the added block shares no
    line with the old file, so difflib matches the system lines around it and
    reports the gap between them as a pure insert. `replace` is a different
    shape and gets its own test below."""
    system = notes_fixture(REAL_SYSTEM_PROSE).splitlines(keepends=True)
    half = len(system) // 2
    seed_notes(root, "".join(system))
    inserted = (
        "".join(system[:half])
        + "\n"
        + notes_fixture(REAL_PLAY_PROSE[1])
        + "\n"
        + "".join(system[half:])
    )
    (root / NOTES).write_text(inserted, encoding="utf-8")
    assert opcode_tags("".join(system), inserted) == ["equal", "insert", "equal"]

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (0, "admitted"), result


def test_a_replace_span_between_system_lines_adds_only_the_rewritten_lines(
    capsys, root, bare, isolated, render_log
):
    """The other opcode `added_lines` walks, actually produced: one accepted
    passage overwritten by another between pre-existing system lines.

    difflib reports `replace` here rather than `insert` because the old lines
    in that span are gone as well, and the span carries an a-side and a b-side
    of different lengths. The gate has to take the b-side - what the file says
    now - and nothing on either side of it. Read from the a-indices instead,
    or from the outer bounds of the changed region, and the untouched system
    lines around the rewrite come back as this proposal's own new text."""
    system = notes_fixture(REAL_SYSTEM_PROSE).splitlines(keepends=True)
    half = len(system) // 2
    head, tail = "".join(system[:half]), "".join(system[half:])
    was, now = notes_fixture(REAL_PLAY_PROSE[1]), notes_fixture(REAL_PLAY_PROSE[2])
    before = head + "\n" + was + "\n" + tail
    rewritten = head + "\n" + now + "\n" + tail
    seed_notes(root, before)
    (root / NOTES).write_text(rewritten, encoding="utf-8")
    assert opcode_tags(before, rewritten) == ["equal", "replace", "equal"]

    # The b-side of that span, exactly: the rewrite, and not one system line.
    assert content_gate.added_lines(before, rewritten) == passages(now)

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (0, "admitted"), result
    assert landed(bare)


def test_system_prose_pasted_over_a_passage_is_read_through_the_replace_span(
    capsys, root, bare, isolated, render_log
):
    """The same opcode in the other direction, which is what stops the test
    above from passing for the wrong reason: a gate that walked only `insert`
    spans would also add no system line there, and would let the real system
    section in whenever a seat pasted it OVER a passage instead of appending
    it. Three accepted passages are committed; the middle one is replaced by
    the system section."""
    accepted = [notes_fixture(name) for name in REAL_PLAY_PROSE]
    before = "# notes\n\n" + "\n".join(accepted)
    after = "# notes\n\n" + "\n".join([accepted[0], notes_fixture(REAL_SYSTEM_PROSE), accepted[2]])
    seed_notes(root, before)
    (root / NOTES).write_text(after, encoding="utf-8")
    assert "replace" in opcode_tags(before, after)

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "report_problem" in result["message"]
    # Quoted from the span that was replaced in, not from the passages around it.
    assert "kill_turn_mean" in result["message"]
    assert rendered_log(render_log) == []
    assert branches(bare) == {"main"}


def test_deleting_the_pre_existing_system_text_is_proposable(
    capsys, root, bare, isolated, render_log
):
    """The clean-up has to be possible, or the gate has made the violation
    permanent - which is the one outcome worse than having let it in."""
    seed_notes(root, "# notes\n\n" + notes_fixture(REAL_SYSTEM_PROSE))
    (root / NOTES).write_text("# notes\n", encoding="utf-8")

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (0, "admitted"), result


def test_moving_a_pre_existing_matchup_note_to_another_matchup_is_not_new_text(
    capsys, root, bare, isolated, render_log
):
    """A note already in the plan is not new however it moved - the same rule
    as NOTES.md, on the one field the plan lets a seat rewrite."""
    already = "The api 500s on the second fetch, so keep the second removal spell."
    edit_plan(root, lambda doc: doc["matchups"][0]["notes"].append(already))
    git(root, "commit", "-q", "-am", "pre-existing violation")
    git(root, "push", "-q", "origin", "HEAD:main")

    def move(doc):
        doc["matchups"][0]["notes"].remove(already)
        doc["matchups"][1]["notes"] = [already]

    edit_plan(root, move)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result


# -- (d) fail closed, on shapes T38's two cases do not reach ----------


def test_a_committed_matchup_shape_the_gate_cannot_read_freezes_the_deck_closed(
    capsys, root, bare, isolated, render_log
):
    """The corrupt matchup is UNCHANGED, so the plan-key check has nothing to
    say about it and the gate is the first thing that meets it. It refuses:
    a plan it cannot read whole is a plan whose new text it cannot place."""
    edit_plan(root, lambda doc: doc["matchups"].append("not a matchup object"))
    git(root, "commit", "-q", "-am", "a plan that is already the wrong shape")
    git(root, "push", "-q", "origin", "HEAD:main")

    edit_plan(root, lambda doc: doc["matchups"][0]["notes"].append("Bolt the frog on sight."))
    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert "could not decide" in result["message"].lower()
    assert "matchups[2]" in result["message"]
    assert branches(bare) == {"main"}


def test_a_plan_whose_matchups_are_not_a_list_is_undecidable_at_the_gate():
    """The gate's own answer, asserted here because the plan-key check gets to
    this shape first end-to-end (the test below) and the gate is never asked.
    It is still the gate's contract: a document it cannot walk is raised, not
    walked half way and judged on what it found."""
    plan = {"matchups": {"dimir frog": {"notes": ["Bolt the frog."]}}}
    with pytest.raises(content_gate.Undecidable, match="matchups are not a list"):
        content_gate.added_plan_passages({"matchups": []}, plan)
    with pytest.raises(content_gate.Undecidable, match="not a JSON object"):
        content_gate.added_plan_passages({}, [{"notes": []}])


def test_a_plan_whose_matchups_are_not_a_list_is_refused_by_the_key_check_first(
    capsys, root, bare, isolated, render_log
):
    """Which refusal a seat sees matters, because the remedy differs: this one
    is a plan shape the allowlist owns, so it stays the file-path `refused`."""
    plan = json.loads((root / PLAN).read_text(encoding="utf-8"))
    (root / PLAN).write_text(
        dump({**plan, "matchups": {"dimir frog": {"notes": []}}}), encoding="utf-8"
    )
    git(root, "commit", "-q", "-am", "a plan whose matchups are a mapping")
    git(root, "push", "-q", "origin", "HEAD:main")

    edit_plan(root, lambda doc: doc["matchups"].__setitem__("living end", {"notes": ["hate up"]}))
    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, "refused"), result
    assert "matchups" in result["message"]
    assert branches(bare) == {"main"}


def test_the_undecided_refusal_leaves_the_text_alone_and_commits_nothing(
    capsys, monkeypatch, root, bare, isolated, render_log
):
    """Fail-closed still has to obey the same promise the content refusal
    makes: the seat keeps its bytes and nothing reaches origin."""
    calls = interpose(monkeypatch)
    before = main_sha(bare)
    raw = b"# notes\n- \xff\xfe not text\n"
    (root / NOTES).write_bytes(raw)

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, content_gate.CONTENT_REFUSED), result
    assert (root / NOTES).read_bytes() == raw
    argv = [" ".join(c["argv"]) for c in calls]
    assert not [a for a in argv if " commit" in a or " push" in a]
    assert main_sha(bare) == before
    assert rendered_log(render_log) == []


def test_the_undecided_message_routes_to_the_report_tool_and_says_nothing_moved():
    message = content_gate.undecided_message("it is not readable as text")
    assert "could not decide" in message.lower()
    assert "report_problem" in message
    assert "nothing was changed, committed or pushed" in message.lower()
    # It names what it could not read, so the seat can fix the file.
    assert "it is not readable as text" in message


def test_a_play_plan_value_the_gate_cannot_read_as_text_is_undecidable():
    """The gate's own fail-closed promise at the leaf: an unreadable value is
    raised, never skipped over on the way to a verdict."""
    before = {"matchups": [{"play_plan": {"our_tactic": "race"}}]}
    after = {"matchups": [{"play_plan": {"our_tactic": {1, 2}}}]}
    with pytest.raises(content_gate.Undecidable):
        content_gate.added_plan_passages(before, after)


# -- (e) `plays admit` is outside the gate, proved on real corpus intents ----


def test_plays_admit_never_consults_the_content_gate():
    """Structural, so it holds for every one of the corpus's play scripts and
    not only the ones a test happens to run."""
    from magic_cards_model_api.playscripts import admit

    source = Path(admit.__file__).read_text(encoding="utf-8")
    assert "content_gate" not in source
    assert content_gate.CONTENT_REFUSED not in source


def test_the_real_corpus_carries_play_intents_this_gate_would_refuse():
    """The evidence for the exclusion, measured rather than asserted: if this
    ever comes back empty, the exclusion has stopped costing anything and the
    decision is worth revisiting."""
    intents = {
        path.relative_to(CORPUS_PLAYS).as_posix(): json.loads(path.read_text(encoding="utf-8"))[
            "intent"
        ]
        for path in sorted(CORPUS_PLAYS.glob("*/plays/*.json"))
        if path.name != "inputs.json"
    }
    refused = [
        name for name, text in intents.items() if content_gate.system_problem_reasons([text])
    ]
    assert len(refused) >= 10, refused
    assert SYSTEM_PLAY in refused
    limits = [name for name, text in intents.items() if "Limits:" in text]
    assert len(limits) >= 10, limits
    assert LIMITS_PLAY in limits


@pytest.mark.parametrize("relative", [LIMITS_PLAY, SYSTEM_PLAY], ids=["limits-clause", "names-api"])
def test_a_real_corpus_play_intent_is_admitted_however_it_reads(
    capsys, root, bare, isolated, relative
):
    """A real intent out of the corpus, verbatim, on a play `plays admit` has
    to take.

    The two parameters are the two halves of the argument. `limits-clause` is
    the shape the exclusion was written for - a "Limits:" clause naming the
    DSL's own vocabulary gaps, "the vocabulary cannot join a card's mana value
    to the mana actually available" - which this particular gate happens not
    to flag but a naive one refuses on sight. `names-api` is the one that
    needs no imagination: it names the api, the sibling engine and two commit
    SHAs, and `system_problem_reasons` really does refuse it, so an `admit`
    that consulted the gate would reject a play the corpus ships today."""
    intent = corpus_intent(relative)
    play = copy.deepcopy(FETCH_SURVEIL) | {
        "id": "corpus-intent",
        # A window of its own, so this is an intent test and not a conflict one.
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
        "intent": intent,
    }
    (root / f"decks/{SLUG}/plays/corpus-intent.json").write_text(dump(play), encoding="utf-8")

    rc, result, _, _ = admit(capsys, root)

    assert (rc, result["result"]) == (0, "admitted"), result
    # Admitted verbatim: no edit, no truncation on the way through either.
    assert git(root, "show", f"HEAD:decks/{SLUG}/plays/corpus-intent.json").count(intent[:60]) == 1


# -- (f) the file-path allowlist still answers first ----------


FORBIDDEN_PATHS = [
    f"decks/{SLUG}/decklist.txt",
    OLD_PLAN,
    f"decks/{SLUG}/plays/fetch-untapped-shock.json",
    f"decks/{SLUG}/shell.json",
    f"decks/{SLUG}/evidence/run.json",
    GAMEPLAN,
    "meta/card-data.json",
]


@pytest.mark.parametrize("path", FORBIDDEN_PATHS)
def test_a_forbidden_file_carrying_system_text_is_still_the_file_path_refusal(
    capsys, root, bare, isolated, render_log, path
):
    """The two refusals have different remedies - revert the file, versus
    reword the sentence - so a file that is outside the allowlist has to be
    told that, whatever its text says."""
    (root / path).write_text(CANONICAL_VIOLATION, encoding="utf-8")

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, "refused"), result
    assert result["result"] != content_gate.CONTENT_REFUSED
    assert path in result["files"]
    assert "report_problem" not in result["message"]
    assert rendered_log(render_log) == []
    assert branches(bare) == {"main"}


FORBIDDEN_PLAN_KEYS = [
    ("matchups[0].stance", lambda doc: doc["matchups"][0].__setitem__("stance", SYSTEM_NOTE)),
    ("matchups[0].play", lambda doc: doc["matchups"][0].__setitem__("play", {"in": [SYSTEM_NOTE]})),
    ("gaps", lambda doc: doc.__setitem__("gaps", [SYSTEM_NOTE])),
    ("archetype", lambda doc: doc.__setitem__("archetype", SYSTEM_NOTE)),
]


@pytest.mark.parametrize(
    ("where", "change"), FORBIDDEN_PLAN_KEYS, ids=[k[0] for k in FORBIDDEN_PLAN_KEYS]
)
def test_a_forbidden_plan_key_carrying_system_text_is_still_the_file_path_refusal(
    capsys, root, bare, isolated, render_log, where, change
):
    edit_plan(root, change)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert where in result["message"]
    assert "report_problem" not in result["message"]
    assert branches(bare) == {"main"}


def test_the_file_path_refusal_is_reached_before_the_content_gate_runs(
    capsys, monkeypatch, root, bare, isolated, render_log
):
    """ "Before anything runs" said as a test rather than as an ordering in the
    source: the gate is made to explode, and a forbidden file still refuses."""

    def never(*_):
        raise AssertionError("the content gate ran on a proposal the allowlist had already refused")

    monkeypatch.setattr(content_gate, "system_problem_reasons", never)
    monkeypatch.setattr(content_gate, "added_lines", never)
    (root / f"decks/{SLUG}/decklist.txt").write_text(CANONICAL_VIOLATION, encoding="utf-8")

    rc, result, _, _ = propose(capsys, root)

    assert (rc, result["result"]) == (1, "refused"), result
