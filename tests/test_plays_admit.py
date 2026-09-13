"""`mcp-seat plays priority` and `mcp-seat plays admit`: a seat's play-script
change goes from its guides clone onto mtg-decks main, through the gate,
never forced, and never with the token anywhere a log could see it.

Everything runs against tmp bare repos standing in for github.com and a fake
GitHub REST server standing in for the pulls endpoint (see
`guides_git_testkit`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from guides_git_testkit import (
    GAME,
    REPO,
    TOKEN,
    FakeGitHub,
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

from magic_cards_model_api.playscripts.lint import check_report

SHOCK = f"decks/{SLUG}/plays/fetch-untapped-shock.json"
SURVEIL = f"decks/{SLUG}/plays/fetch-surveil-land.json"
DELTAS = Path(".git") / "mtg-plays-deltas.json"


@pytest.fixture
def github(tmp_path, monkeypatch):
    fake = FakeGitHub()
    isolate_git(tmp_path, monkeypatch, fake)
    yield fake
    fake.close()


@pytest.fixture
def bare(tmp_path, github):
    return seed_guides(tmp_path)


@pytest.fixture
def root(tmp_path, bare):
    return clone(bare, tmp_path / "guides")


def priority(capsys, root: Path, play_id: str, *flag: str):
    return cli(capsys, "plays", "priority", play_id, *flag, "--deck", SLUG, "--root", str(root))


def admit(capsys, root: Path, game: str = GAME):
    return cli(capsys, "plays", "admit", "--deck", SLUG, "--game-id", game, "--root", str(root))


def read_play(root: Path, rel: str = SHOCK) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


# -- plays priority ----------


def test_priority_changes_only_the_priority_line_and_records_the_delta(capsys, root):
    before = (root / SHOCK).read_text(encoding="utf-8")
    rc, result, _, _ = priority(capsys, root, "fetch-untapped-shock", "--delta", "10")
    assert rc == 0
    assert (result["old"], result["new"]) == (60, 70)
    after = (root / SHOCK).read_text(encoding="utf-8")
    assert after == before.replace('"priority": 60,', '"priority": 70,')
    deltas = json.loads((root / DELTAS).read_text(encoding="utf-8"))
    assert deltas[SHOCK]["delta"] == 10
    assert (deltas[SHOCK]["old"], deltas[SHOCK]["new"]) == (60, 70)
    # Recorded inside .git, so it is never part of the working-tree change.
    assert git(root, "status", "--porcelain") == f"M {SHOCK}"


def test_priority_clamps_to_0_100_and_accumulates_one_delta_per_play(capsys, root):
    assert priority(capsys, root, "fetch-untapped-shock", "--set", "150")[1]["new"] == 100
    assert priority(capsys, root, "fetch-untapped-shock", "--delta", "-500")[1]["new"] == 0
    deltas = json.loads((root / DELTAS).read_text(encoding="utf-8"))
    assert (deltas[SHOCK]["old"], deltas[SHOCK]["new"], deltas[SHOCK]["delta"]) == (60, 0, -60)
    assert read_play(root) == {**FETCH_SHOCK, "priority": 0}


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
    priority(capsys, root, "fetch-surveil-land", "--set", "20")
    rc, result, _, _ = admit(capsys, root)
    assert rc == 0, result
    assert result["result"] == "admitted"
    assert result["sha"] == main_sha(bare) == git(root, "rev-parse", "HEAD")
    assert git(bare, "log", "-1", "--format=%an|%ae|%cn|%ce", "main") == (
        f"{who} {SLUG}|{who}@mtg-trainer.invalid|{who} {SLUG}|{who}@mtg-trainer.invalid"
    )
    assert trailers(bare, "main", "Game-Id") == [GAME]
    assert sorted(trailers(bare, "main", "Script-Priority")) == [
        "fetch-surveil-land 50->20",
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


# -- non-fast-forward: retry, then a pull request ---------------------------------------


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


def test_three_non_fast_forwards_then_a_pull_request(
    capsys, monkeypatch, tmp_path, root, bare, github
):
    pushes: list[list[str]] = []

    def race(argv):
        if is_main_push(argv):
            pushes.append(argv)
            compete(bare, tmp_path / "competitor", f"race {len(pushes)}")

    calls = interpose(monkeypatch, race)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, out, err = admit(capsys, root)
    assert (rc, result["result"]) == (0, "pr_opened"), result
    assert len(pushes) == 3
    branch = f"seat/{GAME}-plays"
    assert result["branch"] == branch
    assert result["pr_url"] == f"https://github.com/{REPO}/pull/1"
    assert branch in branches(bare)
    assert trailers(bare, branch, "Game-Id") == [GAME]
    assert git(bare, "log", "-1", "--format=%s", "main") == "competitor race 3"
    (pull,) = github.pulls()
    assert pull["path"] == f"/repos/{REPO}/pulls"
    assert (pull["body"]["head"], pull["body"]["base"]) == (branch, "main")
    assert pull["headers"]["authorization"] == f"Bearer {TOKEN}"
    # Local main is back on origin/main: the change lives on the pushed branch.
    assert git(root, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/main")
    assert git(root, "rev-parse", branch) == git(bare, "rev-parse", branch)

    # (4) the token: read through the credential helper, never printed, never
    # in argv or a subprocess env, never in .git/config or a commit.
    assert TOKEN not in out + err
    assert all(TOKEN not in " ".join(c["argv"]) for c in calls)
    assert all(TOKEN not in json.dumps(c["env"]) for c in calls)
    assert TOKEN not in (root / ".git" / "config").read_text(encoding="utf-8")
    assert TOKEN not in git(root, "log", "--all", "-p")
    assert ["git", "credential", "fill"] in [c["argv"][:3] for c in calls]


# -- rebase: priority deltas replay, content conflicts do not ----------


def test_two_clones_changing_one_priority_are_auto_resolved_by_deltas(capsys, tmp_path, root, bare):
    other = clone(bare, tmp_path / "guides-b")
    original = (root / SHOCK).read_text(encoding="utf-8")
    priority(capsys, root, "fetch-untapped-shock", "--delta", "10")
    assert admit(capsys, root)[1]["result"] == "admitted"

    priority(capsys, other, "fetch-untapped-shock", "--delta", "-20")
    rc, result, _, _ = admit(capsys, other)
    assert (rc, result["result"]) == (0, "admitted"), result
    merged = git(bare, "show", f"main:{SHOCK}") + "\n"
    assert merged == original.replace('"priority": 60,', '"priority": 50,')
    assert trailers(bare, "main", "Script-Priority") == ["fetch-untapped-shock 70->50"]
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
def test_a_gate_failure_pushes_nothing(capsys, monkeypatch, tmp_path, github, failure):
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
    assert github.pulls() == []


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


def test_raising_a_disabled_duplicate_into_conflict_is_refused(capsys, tmp_path, github):
    bare = seed_guides(tmp_path, (FETCH_SHOCK, FETCH_SURVEIL, play(id="spare", priority=0)))
    root = clone(bare, tmp_path / "guides")
    before = main_sha(bare)
    priority(capsys, root, "spare", "--set", "30")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "conflict_regression"), result
    assert [sorted(c["plays"]) for c in result["new_conflicts"]] == [
        ["fetch-untapped-shock", "spare"]
    ]
    assert main_sha(bare) == before


def test_a_new_next_conflict_is_refused(capsys, tmp_path, github):
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


def test_an_unrelated_change_beside_a_pre_existing_conflict_is_admitted(capsys, tmp_path, github):
    bare = seed_guides(tmp_path, (play(id="a"), play(id="b"), FETCH_SURVEIL))
    before = conflicts_on_main(bare, tmp_path, "before")
    assert before == [("play-conflict", ["a", "b"])]
    root = clone(bare, tmp_path / "guides")
    priority(capsys, root, "fetch-surveil-land", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert conflicts_on_main(bare, tmp_path, "after") == before


def test_resolving_a_conflict_by_extending_a_condition_is_admitted(capsys, tmp_path, github):
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


def test_a_clone_without_card_data_fails_closed(capsys, tmp_path, github):
    bare = seed_guides(tmp_path, card_data=False)
    root = clone(bare, tmp_path / "guides")
    before = main_sha(bare)
    priority(capsys, root, "fetch-untapped-shock", "--delta", "5")
    rc, result, _, _ = admit(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert "card-data.json" in (result["message"] + result.get("output", "")), result
    assert main_sha(bare) == before
    assert branches(bare) == {"main"}
    assert github.pulls() == []
