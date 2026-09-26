"""`mcp-seat plays priority` and `mcp-seat plays admit`: a seat's play-script
change goes from its guides clone onto mtg-decks main, through the gate,
never forced, and never with the token anywhere a log could see it.

Everything runs against tmp bare repos standing in for github.com (see
`guides_git_testkit`). After three lost races the admit is a retryable
`error` with the change left in the tree.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from guides_git_testkit import (
    GAME,
    TOKEN,
    branches,
    cli,
    clone,
    compete,
    dump,
    git,
    interpose,
    is_main_push,
    isolate_git,
    main_sha,
    seed_guides,
    trailers,
)
from playscript_testkit import FETCH_SHOCK, FETCH_SURVEIL, SLUG, play

from magic_cards_model_api.playscripts import admit as admit_mod
from magic_cards_model_api.playscripts import gitwork
from magic_cards_model_api.playscripts.lint import check_report

SHOCK = f"decks/{SLUG}/plays/fetch-untapped-shock.json"
SURVEIL = f"decks/{SLUG}/plays/fetch-surveil-land.json"
DELTAS = Path(".git") / "mtg-plays-deltas.json"
JOURNAL = Path(".git") / "mcm-results.jsonl"


@pytest.fixture(autouse=True)
def _no_ambient_game(monkeypatch):
    """`plays priority` reads MTG_GAME_ID for its per-game bound; a test sets
    it when it means to; `plays admit` reads MTG_EVIDENCE_DIR for the driver's
    evidence file, likewise."""
    monkeypatch.delenv("MTG_GAME_ID", raising=False)
    monkeypatch.delenv("MTG_EVIDENCE_DIR", raising=False)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    return isolate_git(tmp_path, monkeypatch)


@pytest.fixture
def bare(tmp_path, isolated):
    return seed_guides(tmp_path)


@pytest.fixture
def root(tmp_path, bare):
    return clone(bare, tmp_path / "guides")


def priority(capsys, root: Path, play_id: str, *flag: str):
    # Parity 4: `plays priority` now fails loudly with neither `--game-id`
    # nor MTG_GAME_ID set, so every caller that does not itself supply
    # `--game-id` (to test the per-game bound against a game of its own
    # choosing) gets the suite's own `GAME` by default here.
    if "--game-id" not in flag:
        flag = (*flag, "--game-id", GAME)
    return cli(capsys, "plays", "priority", play_id, *flag, "--deck", SLUG, "--root", str(root))


def admit(capsys, root: Path, game: str = GAME):
    return cli(capsys, "plays", "admit", "--deck", SLUG, "--game-id", game, "--root", str(root))


def journal(root: Path) -> list[dict]:
    """Every line of the checkout's results journal, oldest first."""
    path = root / JOURNAL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def last_entry(root: Path) -> dict:
    entries = journal(root)
    assert entries, "the command appended no journal line"
    return entries[-1]


def read_play(root: Path, rel: str = SHOCK) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


# -- plays priority ----------


def test_priority_changes_only_the_priority_line_and_records_the_delta(capsys, root):
    before = (root / SHOCK).read_text(encoding="utf-8")
    rc, result, _, _ = priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    assert rc == 0
    assert (result["old"], result["new"]) == (60, 65)
    after = (root / SHOCK).read_text(encoding="utf-8")
    assert after == before.replace('"priority": 60,', '"priority": 65,')
    deltas = json.loads((root / DELTAS).read_text(encoding="utf-8"))
    assert deltas[SHOCK]["delta"] == 5
    assert (deltas[SHOCK]["old"], deltas[SHOCK]["new"]) == (60, 65)
    # Recorded inside .git, so it is never part of the working-tree change.
    assert git(root, "status", "--porcelain") == f"M {SHOCK}"


def _hand_set(root: Path, value: int) -> None:
    text = (root / SHOCK).read_text(encoding="utf-8")
    (root / SHOCK).write_text(text.replace('"priority": 60,', f'"priority": {value},'), "utf-8")


def test_priority_clamps_to_0_100(capsys, root):
    _hand_set(root, 98)
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "5")[1]["new"] == 100
    (root / DELTAS).unlink()
    text = (root / SHOCK).read_text(encoding="utf-8")
    (root / SHOCK).write_text(text.replace('"priority": 100,', '"priority": 2,'), "utf-8")
    # A different game, so its own per-game allowance starts fresh from 2
    # rather than being bound by the first call's game (which found it at 98).
    assert (
        priority(capsys, root, "fetch-untapped-shock", "--delta", "-5", "--game-id", "g-clamp")[1][
            "new"
        ]
        == 0
    )


def test_priority_accumulates_one_delta_per_play(capsys, root):
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "3")[1]["new"] == 63
    assert priority(capsys, root, "fetch-untapped-shock", "--set", "58")[1]["new"] == 58
    deltas = json.loads((root / DELTAS).read_text(encoding="utf-8"))
    assert (deltas[SHOCK]["old"], deltas[SHOCK]["new"], deltas[SHOCK]["delta"]) == (60, 58, -2)
    assert read_play(root) == {**FETCH_SHOCK, "priority": 58}


# Game 16a9cf8f: one handover lowered two fetch plays by 20 on no evidence.
# A priority moves at most 5 per call, and - when the game is known, from
# `--game-id` or the pod's MTG_GAME_ID - at most 5 per game.


@pytest.mark.parametrize(
    "flag", [("--delta", "6"), ("--delta", "-20"), ("--set", "66"), ("--set", "54")]
)
def test_priority_refuses_a_change_over_5_in_one_call(capsys, monkeypatch, root, flag):
    monkeypatch.delenv("MTG_GAME_ID", raising=False)
    before = (root / SHOCK).read_text(encoding="utf-8")
    rc, result, _, _ = priority(capsys, root, "fetch-untapped-shock", *flag)
    assert rc == 1
    assert result["result"] == "priority_limit"
    assert result["limit"] == 5
    assert "at most 5" in result["message"]
    assert (root / SHOCK).read_text(encoding="utf-8") == before
    assert not (root / DELTAS).exists()


def test_priority_refuses_a_second_call_that_takes_one_game_past_5(capsys, monkeypatch, root):
    monkeypatch.delenv("MTG_GAME_ID", raising=False)
    game = ("--game-id", "g-1")
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "-3", *game)[1]["new"] == 57
    rc, result, _, _ = priority(capsys, root, "fetch-untapped-shock", "--delta", "-3", *game)
    assert rc == 1
    assert result["result"] == "priority_limit"
    assert "this game" in result["message"]
    assert read_play(root)["priority"] == 57
    # Back up and past the start again is still within 5 of where the game began.
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "5", *game)[1]["new"] == 62
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "4", *game)[0] == 1
    # Another game starts its own allowance from where the priority is now.
    assert (
        priority(capsys, root, "fetch-untapped-shock", "--delta", "5", "--game-id", "g-2")[1]["new"]
        == 67
    )


def priority_no_flag(capsys, root: Path, play_id: str, *flag: str):
    """`priority()` without the auto-injected `--game-id` - for the one test
    that means to prove the MTG_GAME_ID env fallback still works on its own."""
    return cli(capsys, "plays", "priority", play_id, *flag, "--deck", SLUG, "--root", str(root))


def test_priority_reads_the_game_from_mtg_game_id(capsys, monkeypatch, root):
    monkeypatch.setenv("MTG_GAME_ID", "g-env")
    assert priority_no_flag(capsys, root, "fetch-untapped-shock", "--delta", "4")[0] == 0
    rc, result, _, _ = priority_no_flag(capsys, root, "fetch-untapped-shock", "--delta", "2")
    assert (rc, result["result"]) == (1, "priority_limit")


def test_priority_refuses_loudly_with_neither_flag_nor_env(capsys, monkeypatch, root):
    monkeypatch.delenv("MTG_GAME_ID", raising=False)
    before = (root / SHOCK).read_text(encoding="utf-8")
    rc, result, _, _ = priority_no_flag(capsys, root, "fetch-untapped-shock", "--delta", "4")
    assert rc == 1
    assert result["result"] == "refused"
    assert "game-id" in result["message"] or "MTG_GAME_ID" in result["message"]
    # Nothing was touched: a loud refusal before any write, not a priority
    # move that merely forgot to record itself.
    assert (root / SHOCK).read_text(encoding="utf-8") == before
    assert not (root / DELTAS).exists()


def test_the_per_game_allowance_survives_an_admit(capsys, monkeypatch, root, bare):
    monkeypatch.delenv("MTG_GAME_ID", raising=False)
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "5", "--game-id", GAME)[0] == 0
    assert admit(capsys, root)[1]["result"] == "admitted"
    rc, result, _, _ = priority(
        capsys, root, "fetch-untapped-shock", "--delta", "1", "--game-id", GAME
    )
    assert (rc, result["result"]) == (1, "priority_limit")
    assert git(root, "status", "--porcelain") == ""


def test_priority_needs_exactly_one_of_delta_or_set(capsys, root):
    with pytest.raises(SystemExit):
        priority(capsys, root, "fetch-untapped-shock", "--delta", "1", "--set", "4")
    assert "argument --set: not allowed with argument --delta" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        priority(capsys, root, "fetch-untapped-shock")
    assert "one of the arguments --delta --set is required" in capsys.readouterr().err


def test_priority_of_an_unknown_play_fails(capsys, root):
    rc, result, _, _ = priority(capsys, root, "no-such-play", "--delta", "1")
    assert rc == 1
    assert "no-such-play" in result["message"]


# -- plays admit: the happy path, identity, trailers ----------


@pytest.mark.parametrize(("role", "who"), [("seat", "mtg-seat"), ("advisor", "mtg-buddy")])
def test_clean_push_with_identity_and_trailers(capsys, monkeypatch, root, bare, role, who):
    monkeypatch.setenv("MTG_ROLE", role)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    priority(capsys, root, "fetch-surveil-land", "--set", "45")
    rc, result, _, _ = admit(capsys, root)
    assert rc == 0, result
    assert result["result"] == "admitted"
    assert result["sha"] == main_sha(bare) == git(root, "rev-parse", "HEAD")
    assert git(bare, "log", "-1", "--format=%an|%ae|%cn|%ce", "main") == (
        f"{who} {SLUG}|{who}@mtg-trainer.invalid|{who} {SLUG}|{who}@mtg-trainer.invalid"
    )
    assert trailers(bare, "main", "Game-Id") == [GAME]
    assert sorted(trailers(bare, "main", "Script-Priority")) == [
        "fetch-surveil-land 50->45",
        "fetch-untapped-shock 60->65",
    ]
    assert not (root / DELTAS).exists()


def test_a_new_play_file_is_admitted(capsys, root, bare):
    put_play = copy.deepcopy(FETCH_SURVEIL) | {
        "id": "fetch-late",
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
    }
    (root / f"decks/{SLUG}/plays/fetch-late.json").write_text(dump(put_play), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert f"decks/{SLUG}/plays/fetch-late.json" in git(
        bare, "ls-tree", "-r", "--name-only", "main"
    )


def test_nothing_to_admit(capsys, root, bare):
    before = main_sha(bare)
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit")
    assert main_sha(bare) == before


@pytest.mark.parametrize(
    "outside",
    [
        f"decks/{SLUG}/decklist.txt",
        "meta/card-data.json",
        "scripts/check_plays.py",
        f"decks/{SLUG}/NOTES.md",
        "stray.txt",
    ],
)
def test_a_change_outside_plays_is_refused(capsys, root, bare, outside):
    before, head = main_sha(bare), git(root, "rev-parse", "HEAD")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    target = root / outside
    target.write_text("changed\n", encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "refused")
    assert result["files"] == [outside]
    assert main_sha(bare) == before
    assert git(root, "rev-parse", "HEAD") == head


def test_an_invalid_game_id_is_refused(capsys, root):
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root, game="../main")
    assert (rc, result["result"]) == (1, "refused")


# -- non-fast-forward: retry, then a retryable error -------------------------------------


def test_non_fast_forward_then_success(capsys, monkeypatch, tmp_path, root, bare):
    pushes: list[list[str]] = []

    def race(argv):
        if is_main_push(argv):
            pushes.append(argv)
            if len(pushes) == 1:
                compete(bare, tmp_path / "competitor", "first")

    interpose(monkeypatch, race)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert len(pushes) == 2
    assert result["sha"] == main_sha(bare)
    assert git(bare, "log", "-1", "--format=%s", "main~1") == "competitor first"
    assert read_play(root)["priority"] == 65


def test_three_non_fast_forwards_are_a_retryable_error_with_the_change_left_in_the_tree(
    capsys, monkeypatch, tmp_path, root, bare
):
    pushes: list[list[str]] = []
    racing = [True]

    def race(argv):
        if is_main_push(argv) and racing[0]:
            pushes.append(argv)
            compete(bare, tmp_path / "competitor", f"race {len(pushes)}")

    calls = interpose(monkeypatch, race)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, out, err = admit(capsys, root)
    assert (rc, result["result"]) == (1, "error"), result
    assert result["retryable"] is True
    assert "pr_url" not in result and "branch" not in result
    assert len(pushes) == 3
    # No fallback branch: origin has main only, carrying the races.
    assert branches(bare) == {"main"}
    assert git(bare, "log", "-1", "--format=%s", "main") == "competitor race 3"
    # The commit is soft-reset: the change is back in the tree, uncommitted,
    # on top of the newest upstream this admit fetched.
    assert git(root, "rev-parse", "HEAD") == git(bare, "rev-parse", "main~1")
    assert git(root, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/main")
    assert read_play(root)["priority"] == 65
    assert [line[3:] for line in git(root, "status", "--porcelain").splitlines()] == [SHOCK]
    assert not (root / ".git" / "rebase-merge").exists()

    # The GitHub REST API is never asked for anything.
    assert ["git", "credential", "fill"] not in [c["argv"][:3] for c in calls]
    assert TOKEN not in out + err

    # Run again once main is quiet, and it lands as one commit.
    racing[0] = False
    rc, again, _, _ = admit(capsys, root)
    assert (rc, again["result"]) == (0, "admitted"), again
    assert trailers(bare, "main", "Script-Priority") == ["fetch-untapped-shock 60->65"]


# -- rebase: priority deltas replay, content conflicts do not ----------


def test_two_clones_changing_one_priority_are_auto_resolved_by_deltas(capsys, tmp_path, root, bare):
    other = clone(bare, tmp_path / "guides-b")
    original = (root / SHOCK).read_text(encoding="utf-8")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    assert admit(capsys, root)[1]["result"] == "admitted"

    priority(capsys, other, "fetch-untapped-shock", "--delta", "-3")
    rc, result, _, _ = admit(capsys, other)
    assert (rc, result["result"]) == (0, "admitted"), result
    merged = git(bare, "show", f"main:{SHOCK}") + "\n"
    assert merged == original.replace('"priority": 60,', '"priority": 62,')
    assert trailers(bare, "main", "Script-Priority") == ["fetch-untapped-shock 65->62"]
    assert not (other / DELTAS).exists()


def test_a_content_conflict_is_returned_with_the_commit_intact(capsys, tmp_path, root, bare):
    other = clone(bare, tmp_path / "guides-b")
    for where, text in ((root, "Thin on my main phase."), (other, "Fix on my main phase.")):
        (where / SHOCK).write_text(dump(play(intent=text)), encoding="utf-8")
    assert admit(capsys, root)[1]["result"] == "admitted"
    landed = main_sha(bare)

    rc, result, _, _ = admit(capsys, other)
    assert (rc, result["result"]) == (1, "conflict")
    assert result["files"] == [SHOCK]
    assert main_sha(bare) == landed
    assert trailers(other, "HEAD", "Game-Id") == [GAME]
    assert read_play(other)["intent"] == "Fix on my main phase."
    assert not (other / ".git" / "rebase-merge").exists()
    assert not (other / ".git" / "rebase-apply").exists()
    assert git(other, "status", "--porcelain") == ""


# -- the gate ----------


@pytest.mark.parametrize("failure", ["check_plays_fails", "check_plays_missing", "plays_check"])
def test_a_gate_failure_pushes_nothing(capsys, monkeypatch, tmp_path, isolated, failure):
    bare = seed_guides(tmp_path, check_plays=failure != "check_plays_missing")
    root = clone(bare, tmp_path / "guides")
    before = main_sha(bare)
    if failure == "check_plays_fails":
        monkeypatch.setenv("MTG_TEST_GATE_FAIL", "1")
    if failure == "plays_check":
        (root / SHOCK).write_text(dump(play(priority=60, repeat=3)), encoding="utf-8")
    else:
        priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert main_sha(bare) == before
    assert branches(bare) == {"main"}


# -- the conflict ratchet (owner addendum) ----------


def conflicts_on_main(bare: Path, tmp_path: Path, name: str) -> list[tuple[str, list[str]]]:
    fresh = clone(bare, tmp_path / name)
    return [(w.code, w.plays) for w in check_report(fresh)[1]]


def test_a_change_adding_a_conflicting_pair_is_refused(capsys, root, bare):
    before = main_sha(bare)
    duplicate = play(id="fetch-shock-copy")
    (root / f"decks/{SLUG}/plays/fetch-shock-copy.json").write_text(dump(duplicate), "utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "conflict_regression"), result
    assert [(c["code"], sorted(c["plays"])) for c in result["new_conflicts"]] == [
        ("play-conflict", ["fetch-shock-copy", "fetch-untapped-shock"])
    ]
    assert "extend" in result["message"] and "priority" in result["message"]
    assert "fetch-shock-copy" in result["message"]
    assert main_sha(bare) == before


def test_raising_a_disabled_duplicate_into_conflict_is_refused(capsys, tmp_path, isolated):
    bare = seed_guides(tmp_path, (FETCH_SHOCK, FETCH_SURVEIL, play(id="spare", priority=0)))
    root = clone(bare, tmp_path / "guides")
    before = main_sha(bare)
    priority(capsys, root, "spare", "--set", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "conflict_regression"), result
    assert [sorted(c["plays"]) for c in result["new_conflicts"]] == [
        ["fetch-untapped-shock", "spare"]
    ]
    assert main_sha(bare) == before


def test_a_new_next_conflict_is_refused(capsys, tmp_path, isolated):
    spare_a = play(id="spare-a", priority=0)
    spare_b = play(id="spare-b", priority=0)
    chain = play(
        id="chain",
        priority=0,
        when={**FETCH_SHOCK["when"], "whose_turn": "theirs"},
        next=["spare-a"],
    )
    bare = seed_guides(tmp_path, (FETCH_SURVEIL, spare_a, spare_b, chain))
    root = clone(bare, tmp_path / "guides")
    chained = {**chain, "next": ["spare-a", "spare-b"]}
    (root / f"decks/{SLUG}/plays/chain.json").write_text(dump(chained), "utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "conflict_regression"), result
    assert [(c["code"], c["plays"]) for c in result["new_conflicts"]] == [
        ("next-conflict", ["chain", "spare-a", "spare-b"])
    ]


def test_an_unrelated_change_beside_a_pre_existing_conflict_is_admitted(capsys, tmp_path, isolated):
    bare = seed_guides(tmp_path, (play(id="a"), play(id="b"), FETCH_SURVEIL))
    before = conflicts_on_main(bare, tmp_path, "before")
    assert before == [("play-conflict", ["a", "b"])]
    root = clone(bare, tmp_path / "guides")
    priority(capsys, root, "fetch-surveil-land", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert conflicts_on_main(bare, tmp_path, "after") == before


def test_resolving_a_conflict_by_extending_a_condition_is_admitted(capsys, tmp_path, isolated):
    bare = seed_guides(tmp_path, (play(id="a"), play(id="b"), FETCH_SURVEIL))
    root = clone(bare, tmp_path / "guides")
    b = play(id="b")
    # a's "true" list still requires life >= 8 (unchanged). Narrowing b's own
    # life condition to <= 5 makes the pair's life ranges disjoint - the
    # numeric_range dimension proves them exclusive - without b contradicting
    # its own list the way an added, opposite life >= 8 condition would.
    b["true"] = [b["true"][0], {"kind": "life", "side": "me", "op": "<=", "n": 5}]
    (root / f"decks/{SLUG}/plays/b.json").write_text(dump(b), "utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert conflicts_on_main(bare, tmp_path, "after") == []


# -- retire-and-add in one admit, at the cap (A4) ----------------------------


def test_retire_and_add_in_one_admit_is_admitted(capsys, tmp_path, isolated):
    """A working tree that deletes one play and adds another in the same
    admit lands both in one push. The deleted play (`p0`) carries a
    pre-existing play-conflict with `p1`; that warning must vanish once `p0`
    is gone, never be counted as a newly introduced one. The play-count caps
    (`too-many-plays` et al.) are removed (owner, 2026-09-24), so a deck's
    play count is no longer a reason to retire before adding - this test
    keeps a modest filler count only to exercise the retire-and-add path."""
    conflicting = play(id="p0"), play(id="p1")
    filler = tuple(play(id=f"p{i}", priority=0) for i in range(2, 16))
    plays = (*conflicting, *filler)
    bare = seed_guides(tmp_path, plays)
    before = conflicts_on_main(bare, tmp_path, "before")
    assert before == [("play-conflict", ["p0", "p1"])]

    root = clone(bare, tmp_path / "guides")
    git(root, "rm", "-q", f"decks/{SLUG}/plays/p0.json")
    added = play(id="p-new", priority=0)
    (root / f"decks/{SLUG}/plays/p-new.json").write_text(dump(added), encoding="utf-8")

    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["added"] == ["p-new"]
    assert result["updated"] == ["p0"]
    assert result["reprioritized"] == []

    tracked = git(bare, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert f"decks/{SLUG}/plays/p0.json" not in tracked
    assert f"decks/{SLUG}/plays/p-new.json" in tracked
    play_files = [
        p for p in tracked if p.startswith(f"decks/{SLUG}/plays/") and p.endswith(".json")
    ]
    assert len(play_files) == len(plays)

    # The deletion landed in the pushed commit, not merely in the working
    # tree (--no-renames: p0 and p-new are similar enough that git's default
    # rename detection would report one R line instead of a D and an A).
    changed = git(bare, "diff", "--no-renames", "--name-status", "main~1", "main").splitlines()
    assert f"D\tdecks/{SLUG}/plays/p0.json" in changed
    assert f"A\tdecks/{SLUG}/plays/p-new.json" in changed

    # The conflict tied to the deleted play vanished; nothing new appeared.
    assert conflicts_on_main(bare, tmp_path, "after") == []


def test_a_clone_without_card_data_fails_closed(capsys, tmp_path, isolated):
    bare = seed_guides(tmp_path, card_data=False)
    root = clone(bare, tmp_path / "guides")
    before = main_sha(bare)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert "card-data.json" in (result["message"] + result.get("output", "")), result
    assert main_sha(bare) == before
    assert branches(bare) == {"main"}


# -- the results journal (D2a) ----------------------------------------------
#
# Every `plays priority`, `plays admit` and `plays no-change` run appends one
# JSON line to `<git-dir>/mcm-results.jsonl`, which the driver reads through
# its read-only /guides mount to decide whether the handover did its DSL work.


def test_an_admitted_priority_change_is_journaled_without_a_dsl_change(
    capsys, monkeypatch, root, bare
):
    monkeypatch.setenv("MTG_ROLE", "advisor")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    set_line = last_entry(root)
    assert set_line["command"] == "priority"
    assert set_line["result"] == "priority_set"
    assert set_line["decks"] == [SLUG]
    assert set_line["role"] == "advisor"
    assert set_line["files"] == [SHOCK]
    assert set_line["dsl_change"] is False
    assert set_line["game_id"] == GAME
    assert isinstance(set_line["ts"], str) and set_line["ts"]

    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["dsl_change"] is False
    line = last_entry(root)
    assert {k: line[k] for k in ("command", "game_id", "decks", "role", "result", "sha")} == {
        "command": "admit",
        "game_id": GAME,
        "decks": [SLUG],
        "role": "advisor",
        "result": "admitted",
        "sha": main_sha(bare),
    }
    assert line["files"] == [SHOCK]
    assert line["dsl_change"] is False
    assert len(journal(root)) == 2


def test_a_new_play_is_a_dsl_change(capsys, root):
    added = copy.deepcopy(FETCH_SURVEIL) | {
        "id": "fetch-late",
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
    }
    (root / f"decks/{SLUG}/plays/fetch-late.json").write_text(dump(added), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    line = last_entry(root)
    assert line["dsl_change"] is True
    assert line["files"] == [f"decks/{SLUG}/plays/fetch-late.json"]


def test_a_retired_play_is_a_dsl_change(capsys, root, bare):
    git(root, "rm", "-q", SURVEIL)
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert SURVEIL not in git(bare, "ls-tree", "-r", "--name-only", "main")
    assert last_entry(root)["dsl_change"] is True


def test_a_condition_change_beside_a_priority_change_is_a_dsl_change(capsys, root):
    doc = read_play(root)
    doc["priority"] = 62
    doc["intent"] = "Crack the fetch for the shock only with a two-drop in hand."
    (root / SHOCK).write_text(dump(doc), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert last_entry(root)["dsl_change"] is True


def test_a_reformatted_play_with_only_its_priority_moved_is_no_dsl_change(capsys, root):
    doc = read_play(root)
    doc["priority"] = 63
    (root / SHOCK).write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert last_entry(root)["dsl_change"] is False


# -- added / updated / reprioritized: `_landing`'s per-play classification --


def test_a_new_play_is_classified_as_added(capsys, root):
    added = copy.deepcopy(FETCH_SURVEIL) | {
        "id": "fetch-late",
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
    }
    (root / f"decks/{SLUG}/plays/fetch-late.json").write_text(dump(added), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["added"] == ["fetch-late"]
    assert result["updated"] == []
    assert result["reprioritized"] == []
    line = last_entry(root)
    assert line["added"] == ["fetch-late"]
    assert line["updated"] == []
    assert line["reprioritized"] == []


def test_a_deleted_play_is_classified_as_updated(capsys, root, bare):
    git(root, "rm", "-q", SURVEIL)
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["updated"] == ["fetch-surveil-land"]
    assert result["added"] == []
    assert result["reprioritized"] == []
    assert last_entry(root)["updated"] == ["fetch-surveil-land"]


def test_a_condition_change_is_classified_as_updated_not_reprioritized(capsys, root):
    doc = read_play(root)
    doc["priority"] = 62
    doc["intent"] = "Crack the fetch for the shock only with a two-drop in hand."
    (root / SHOCK).write_text(dump(doc), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["updated"] == ["fetch-untapped-shock"]
    assert result["added"] == []
    assert result["reprioritized"] == []


def test_a_priority_only_change_is_classified_as_reprioritized(capsys, root):
    doc = read_play(root)
    doc["priority"] = 63
    (root / SHOCK).write_text(dump(doc), encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["reprioritized"] == ["fetch-untapped-shock"]
    assert result["added"] == []
    assert result["updated"] == []
    line = last_entry(root)
    assert line["reprioritized"] == ["fetch-untapped-shock"]


def test_added_updated_and_reprioritized_are_all_reported_from_one_admit(capsys, root):
    # priority-only -> reprioritized
    shock = read_play(root)
    shock["priority"] = 63
    (root / SHOCK).write_text(dump(shock), encoding="utf-8")
    # condition change beyond priority -> updated
    surveil = read_play(root, SURVEIL)
    surveil["priority"] = 51
    surveil["intent"] = "Different plan for this game."
    (root / SURVEIL).write_text(dump(surveil), encoding="utf-8")
    # new play -> added
    new_play = copy.deepcopy(FETCH_SURVEIL) | {
        "id": "fetch-late",
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
    }
    (root / f"decks/{SLUG}/plays/fetch-late.json").write_text(dump(new_play), encoding="utf-8")

    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["added"] == ["fetch-late"]
    assert result["updated"] == ["fetch-surveil-land"]
    assert result["reprioritized"] == ["fetch-untapped-shock"]

    line = last_entry(root)
    assert line["added"] == ["fetch-late"]
    assert line["updated"] == ["fetch-surveil-land"]
    assert line["reprioritized"] == ["fetch-untapped-shock"]


def test_nothing_to_admit_carries_empty_landing_lists(capsys, root):
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit")
    assert (result["added"], result["updated"], result["reprioritized"]) == ([], [], [])
    line = last_entry(root)
    assert (line["added"], line["updated"], line["reprioritized"]) == ([], [], [])


def test_nothing_to_admit_is_journaled(capsys, root):
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit")
    line = last_entry(root)
    assert (line["command"], line["result"], line["dsl_change"]) == (
        "admit",
        "nothing_to_admit",
        False,
    )
    assert "sha" not in line


def test_a_refused_admit_is_journaled_with_its_files(capsys, root):
    (root / "stray.txt").write_text("changed\n", encoding="utf-8")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "refused")
    line = last_entry(root)
    assert (line["command"], line["result"], line["files"]) == ("admit", "refused", ["stray.txt"])
    assert "dsl_change" not in line


def test_a_gate_failure_is_journaled(capsys, monkeypatch, root):
    monkeypatch.setenv("MTG_TEST_GATE_FAIL", "1")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed")
    assert last_entry(root)["result"] == "gate_failed"


def test_a_conflict_regression_is_journaled(capsys, root):
    (root / f"decks/{SLUG}/plays/fetch-shock-copy.json").write_text(
        dump(play(id="fetch-shock-copy")), "utf-8"
    )
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "conflict_regression")
    assert last_entry(root)["result"] == "conflict_regression"


def test_a_content_conflict_is_journaled_with_the_local_sha(capsys, tmp_path, root, bare):
    other = clone(bare, tmp_path / "guides-b")
    for where, text in ((root, "Thin on my main phase."), (other, "Fix on my main phase.")):
        (where / SHOCK).write_text(dump(play(intent=text)), encoding="utf-8")
    assert admit(capsys, root)[1]["result"] == "admitted"
    rc, result, _, _ = admit(capsys, other)
    assert (rc, result["result"]) == (1, "conflict")
    line = last_entry(other)
    assert (line["result"], line["files"], line["sha"]) == ("conflict", [SHOCK], result["sha"])


def test_a_retryable_error_is_journaled(capsys, monkeypatch, tmp_path, root, bare):
    def race(argv):
        if is_main_push(argv):
            compete(bare, tmp_path / "competitor", "race")

    interpose(monkeypatch, race)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "error")
    line = last_entry(root)
    assert (line["command"], line["result"], line["retryable"]) == ("admit", "error", True)


def test_a_refused_priority_is_journaled(capsys, root):
    rc, result, _, _ = priority(capsys, root, "fetch-untapped-shock", "--delta", "9")
    assert result["result"] == "priority_limit"
    line = last_entry(root)
    assert (line["command"], line["result"]) == ("priority", "priority_limit")


def test_every_line_is_one_command(capsys, root):
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    admit(capsys, root)
    no_change(capsys, root, "--play", "fetch-surveil-land", "--reason", "it never had a window")
    assert [e["command"] for e in journal(root)] == ["priority", "admit", "no_change"]


def test_a_journal_that_cannot_be_written_never_fails_the_command(capsys, root, bare):
    (root / JOURNAL).mkdir()
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["sha"] == main_sha(bare)


def test_the_journal_is_written_under_the_checkout_lock(capsys, monkeypatch, root):
    from magic_cards_model_api.playscripts import gitwork

    held: list[bool] = []
    real_lock, real_append = gitwork.checkout_lock, gitwork.append_journal_line
    depth = [0]

    import contextlib

    @contextlib.contextmanager
    def counting(*args, **kwargs):
        with real_lock(*args, **kwargs):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    def spy(*args, **kwargs):
        held.append(depth[0] > 0)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(gitwork, "checkout_lock", counting)
    monkeypatch.setattr(gitwork, "append_journal_line", spy)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    admit(capsys, root)
    no_change(capsys, root, "--line", "cast Bolt at their face", "--reason", "one game only")
    assert held == [True, True, True]


# -- plays no-change (D2a) -------------------------------------------------------


def no_change(capsys, root: Path, *flags: str, game: str = GAME):
    return cli(
        capsys,
        "plays",
        "no-change",
        "--deck",
        SLUG,
        "--game-id",
        game,
        *flags,
        "--root",
        str(root),
    )


def test_no_change_for_a_play_is_recorded(capsys, monkeypatch, root, bare):
    before = main_sha(bare)
    rc, result, _, _ = no_change(
        capsys, root, "--play", "fetch-surveil-land", "--reason", "declined once, one game only"
    )
    assert (rc, result["result"]) == (0, "recorded"), result
    line = last_entry(root)
    assert line["command"] == "no_change"
    assert line["game_id"] == GAME and line["decks"] == [SLUG] and line["role"] == "seat"
    assert line["play"] == "fetch-surveil-land"
    assert line["reason"] == "declined once, one game only"
    assert "line" not in line
    assert main_sha(bare) == before
    assert git(root, "status", "--porcelain") == ""


def test_no_change_for_a_hand_played_line_is_recorded(capsys, root):
    rc, result, _, _ = no_change(
        capsys, root, "--line", "fetch at their end step", "--reason", "already the default"
    )
    assert (rc, result["result"]) == (0, "recorded"), result
    line = last_entry(root)
    assert line["line"] == "fetch at their end step" and "play" not in line


@pytest.mark.parametrize(
    "flags",
    [
        ("--reason", "no candidate named"),
        ("--play", "a", "--line", "b", "--reason", "both named"),
        ("--play", "fetch-surveil-land"),
    ],
)
def test_no_change_needs_exactly_one_candidate_and_a_reason(capsys, root, flags):
    with pytest.raises(SystemExit) as exit_:
        no_change(capsys, root, *flags)
    assert exit_.value.code == 2
    assert journal(root) == []


@pytest.mark.parametrize(
    "flags",
    [
        ("--play", "fetch-surveil-land", "--reason", "   "),
        ("--play", "../escape", "--reason", "a path is no play id"),
        ("--line", "  ", "--reason", "an empty line"),
    ],
)
def test_no_change_refuses_a_blank_reason_or_a_bad_play_id(capsys, root, flags):
    rc, result, _, _ = no_change(capsys, root, *flags)
    assert (rc, result["result"]) == (1, "refused"), result
    assert last_entry(root)["result"] == "refused"


def test_no_change_refuses_a_bad_game_id(capsys, root):
    rc, result, _, _ = no_change(capsys, root, "--play", "x", "--reason", "r", game="../main")
    assert (rc, result["result"]) == (1, "refused")


# -- evidence pickup (D2a) -------------------------------------------------------

EVIDENCE = f"decks/{SLUG}/plays/evidence/{GAME}.json"


def evidence_doc() -> dict:
    return {
        "game_id": GAME,
        "deck": SLUG,
        "opponent_archetype": "dimir frog",
        "plays": {
            "fetch-untapped-shock": {
                "offered": 3,
                "taken": 1,
                "passed_over": 1,
                "declined": 1,
            }
        },
    }


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch) -> Path:
    where = tmp_path / "driver-workspace" / "evidence"
    monkeypatch.setenv("MTG_EVIDENCE_DIR", str(where))
    return where


def write_evidence(evidence_dir: Path, text: str, game: str = GAME) -> None:
    target = evidence_dir / game / f"{SLUG}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_the_evidence_file_is_copied_and_pushed_with_the_change(capsys, root, bare, evidence_dir):
    text = json.dumps(evidence_doc(), indent=2) + "\n"
    write_evidence(evidence_dir, text)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert git(bare, "show", f"main:{EVIDENCE}") + "\n" == text
    line = last_entry(root)
    assert sorted(line["files"]) == sorted([SHOCK, EVIDENCE])
    assert line["dsl_change"] is False
    assert result["evidence"] == EVIDENCE


def test_an_admit_with_only_evidence_is_admitted_without_a_dsl_change(
    capsys, root, bare, evidence_dir
):
    write_evidence(evidence_dir, json.dumps(evidence_doc()))
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert EVIDENCE in git(bare, "ls-tree", "-r", "--name-only", "main")
    line = last_entry(root)
    assert (line["result"], line["dsl_change"], line["files"]) == ("admitted", False, [EVIDENCE])
    # Not a play: the per-game file never trips the priority bookkeeping.
    assert trailers(bare, "main", "Script-Priority") == []


def test_evidence_that_is_not_json_is_left_behind(capsys, root, bare, evidence_dir):
    write_evidence(evidence_dir, "{not json")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit"), result
    assert not (root / EVIDENCE).exists()
    assert EVIDENCE not in git(bare, "ls-tree", "-r", "--name-only", "main")


def test_no_evidence_file_changes_nothing(capsys, root, evidence_dir):
    write_evidence(evidence_dir, json.dumps(evidence_doc()), game="another-game")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit"), result
    assert "evidence" not in result


def test_two_games_evidence_never_conflicts(capsys, tmp_path, monkeypatch, root, bare):
    other = clone(bare, tmp_path / "guides-b")
    for where, game in ((root, "game-one"), (other, "game-two")):
        drop = tmp_path / f"evidence-{game}"
        monkeypatch.setenv("MTG_EVIDENCE_DIR", str(drop))
        write_evidence(drop, json.dumps(evidence_doc() | {"game_id": game}), game=game)
        rc, result, _, _ = admit(capsys, where, game=game)
        assert (rc, result["result"]) == (0, "admitted"), result
    tracked = git(bare, "ls-tree", "-r", "--name-only", "main")
    assert f"decks/{SLUG}/plays/evidence/game-one.json" in tracked
    assert f"decks/{SLUG}/plays/evidence/game-two.json" in tracked


def test_the_evidence_file_is_not_linted_as_a_play(capsys, root, evidence_dir):
    write_evidence(evidence_dir, json.dumps(evidence_doc()))
    admit(capsys, root)
    rc, report, _, _ = cli(capsys, "plays", "check", "--root", str(root), "--deck", SLUG, "--json")
    assert rc == 0, report
    assert "evidence" not in json.dumps(report)


# ======================================================================
# FD7 (3): admit stops sweeping the whole checkout into one caller's commit
# (gen gap 10) and stops refusing a whole batch over one conflicting play
# (gen gap 9). The ratchet's own detection is unchanged (every existing
# conflict_regression test above stays green, unmodified): what changes is
# what a caller with several own files gets back.
# ======================================================================


def test_paths_scope_admit_to_only_the_caller_own_files_leaving_the_others_writer_dirty(
    capsys, tmp_path, root, bare
):
    """Two writers share one checkout. Each admits by its own explicit
    `paths` (the primary channel FD7 adds); the other's own uncommitted edit
    is left exactly as it was, ready for its own later admit."""
    a_path = f"decks/{SLUG}/plays/writer-a.json"
    (root / a_path).write_text(dump(play(id="writer-a", priority=0)), encoding="utf-8")
    b_doc = read_play(root, SURVEIL)
    b_doc["priority"] = b_doc["priority"] + 1
    (root / SURVEIL).write_text(dump(b_doc), encoding="utf-8")

    result_a = admit_mod.admit(root, SLUG, "game-a", paths=[a_path])
    assert result_a["result"] == "admitted", result_a
    assert result_a["added"] == ["writer-a"]

    # Writer B's own edit is untouched: still dirty, never swept into A's
    # commit, and not reverted by anything A's admit did. (`git()`'s own
    # `.strip()` eats the leading space `--porcelain` gives an unstaged
    # `M`, the same convention `test_priority_changes_only_the_priority_
    # line_and_records_the_delta` already relies on.)
    assert git(root, "status", "--porcelain") == f"M {SURVEIL}"
    assert json.loads((root / SURVEIL).read_text(encoding="utf-8"))["priority"] == b_doc["priority"]

    result_b = admit_mod.admit(root, SLUG, "game-b", paths=[SURVEIL])
    assert result_b["result"] == "admitted", result_b
    assert result_b["reprioritized"] == ["fetch-surveil-land"]
    assert git(root, "status", "--porcelain") == ""


def test_with_no_explicit_paths_every_dirty_play_path_is_still_staged(capsys, root, bare):
    """Back-compat: every caller before FD7 (and the CLI, which passes no
    `paths`) still gets the old "stage everything under decks/*/plays/"
    behaviour."""
    (root / f"decks/{SLUG}/plays/new-one.json").write_text(
        dump(play(id="new-one", priority=0)), encoding="utf-8"
    )
    result = admit_mod.admit(root, SLUG, GAME)
    assert result["result"] == "admitted"
    assert result["added"] == ["new-one"]


def test_a_priority_journaled_by_a_different_role_is_not_swept_by_this_roles_admit(
    capsys, monkeypatch, root, bare
):
    """The journal fallback (no explicit `paths`) is scoped by MTG_ROLE:
    a priority nudge the SEAT journaled for this game is not this REVIEWER's
    own path, and a reviewer admit with no explicit `paths` must not sweep
    it in - even though it is the only tracked entry for this game at all."""
    monkeypatch.setenv("MTG_ROLE", "seat")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5", "--game-id", GAME)
    # A second, untracked (raw) edit to a different play, made as if by the
    # reviewer sharing the same checkout for the same game.
    monkeypatch.setenv("MTG_ROLE", "reviewer")
    doc = read_play(root, SURVEIL)
    doc["intent"] = "Reviewer's own generalized play."
    (root / SURVEIL).write_text(dump(doc), encoding="utf-8")

    result = admit_mod.admit(root, SLUG, GAME)

    # Nothing tracked for role "reviewer" in this game, so the fallback
    # falls further back to "every dirty play path" - the raw edit lands...
    assert result["result"] == "admitted", result
    assert result["updated"] == ["fetch-surveil-land"]
    # ...but the seat's own priority-only file is ALSO still dirty (its
    # tracked-by-seat change was never staged by the reviewer's own call,
    # since it was never part of `changed` at commit time - only its own
    # untracked file was staged and committed). (`git()`'s own `.strip()`
    # eats the leading space, as above.)
    assert git(root, "status", "--porcelain") == f"M {SHOCK}"


def test_a_role_that_nudged_a_priority_and_also_hand_edited_a_different_play_gets_both(
    capsys, monkeypatch, root, bare
):
    """Review fix (gap 2): the journal used to narrow this role's own sweep
    to exactly its `priority` lines the moment it had ANY of them, dropping
    a second, untracked, hand-edited play by the SAME role - unreported and
    left uncommitted forever, with the CLI's own no-`paths` call (the only
    one production makes) having no other way to recover it."""
    monkeypatch.setenv("MTG_ROLE", "seat")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5", "--game-id", GAME)
    doc = read_play(root, SURVEIL)
    doc["intent"] = "Also this seat's own hand edit, no journal trace of its own."
    (root / SURVEIL).write_text(dump(doc), encoding="utf-8")

    result = admit_mod.admit(root, SLUG, GAME)

    assert result["result"] == "admitted", result
    assert set(result["updated"]) == {"fetch-surveil-land"}
    assert set(result["reprioritized"]) == {"fetch-untapped-shock"}
    assert git(root, "status", "--porcelain") == ""


def test_another_roles_journaled_out_of_scope_file_does_not_block_this_admit(
    capsys, monkeypatch, tmp_path, root, bare
):
    """Review fix (gap 9/10): a `guides propose` that left its own out-of-
    scope file dirty (a `refused_content`/`conflict`, journaled under its
    own role) must not refuse THIS caller's whole `plays admit` - two
    writers sharing a checkout are meant not to block each other."""
    monkeypatch.setenv("MTG_ROLE", "advisor")
    notes = root / f"decks/{SLUG}/NOTES.md"
    with notes.open("a", encoding="utf-8") as handle:
        handle.write("- left dirty by a refused guides propose\n")
    gitwork.append_journal_line(
        root,
        {
            "ts": "2026-01-01T00:00:00Z",
            "command": "propose",
            "game_id": GAME,
            "decks": [SLUG],
            "role": "advisor",
            "result": "refused_content",
            "files": [f"decks/{SLUG}/NOTES.md"],
        },
    )
    monkeypatch.setenv("MTG_ROLE", "seat")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5", "--game-id", GAME)

    result = admit_mod.admit(root, SLUG, GAME)

    assert result["result"] == "admitted", result
    assert result["reprioritized"] == ["fetch-untapped-shock"]
    # The advisor's own out-of-scope file is exactly as it was - never
    # staged, committed or reverted by this call.
    assert git(root, "status", "--porcelain") == f"M {f'decks/{SLUG}/NOTES.md'}"


def test_a_batch_with_one_conflicting_play_admits_the_clean_ones(capsys, tmp_path, isolated):
    """A batch of several own paths, one of which introduces a conflict:
    the clean plays land, the conflicting one is refused and named in the
    report with its reason, and the checkout is left holding only that one
    file's reversion - never the whole batch's."""
    bare = seed_guides(tmp_path, (play(id="existing", priority=10),))
    root = clone(bare, tmp_path / "guides")
    good_path = f"decks/{SLUG}/plays/good.json"
    bad_path = f"decks/{SLUG}/plays/bad.json"
    # "good" gets its own window (theirs/upkeep) so it shares no dimension
    # with "existing" - only "bad", left at "existing"'s own default
    # when/mine/precombat_main, actually conflicts with it.
    good = play(
        id="good", priority=20, when={"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"}
    )
    (root / good_path).write_text(dump(good), encoding="utf-8")
    duplicate = play(id="bad")
    (root / bad_path).write_text(dump(duplicate), encoding="utf-8")

    result = admit_mod.admit(root, SLUG, GAME, paths=[good_path, bad_path])

    assert result["result"] == "admitted", result
    assert result["added"] == ["good"]
    refused = {entry["path"]: entry for entry in result["refused_files"]}
    assert set(refused) == {bad_path}
    assert "bad" in refused[bad_path]["reason"] and "existing" in refused[bad_path]["reason"]

    tracked = git(bare, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert good_path in tracked
    assert bad_path not in tracked
    # The refused file is restored in the working tree too: it never
    # existed upstream, so it is gone, not left half-admitted.
    assert not (root / bad_path).exists()
    assert git(root, "status", "--porcelain") == ""


def test_a_batch_where_every_play_conflicts_still_refuses_the_whole_batch(capsys, tmp_path):
    """When nothing would survive dropping the culprits, the old
    all-or-nothing `conflict_regression` still applies - unchanged from
    every test above this section."""
    bare = seed_guides(tmp_path, (play(id="existing", priority=10),))
    root = clone(bare, tmp_path / "guides")
    before = main_sha(bare)
    duplicate_path = f"decks/{SLUG}/plays/dup.json"
    (root / duplicate_path).write_text(dump(play(id="dup")), encoding="utf-8")

    result = admit_mod.admit(root, SLUG, GAME, paths=[duplicate_path])

    assert result["result"] == "conflict_regression", result
    assert main_sha(bare) == before
    assert (root / duplicate_path).exists()
