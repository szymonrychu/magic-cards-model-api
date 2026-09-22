"""`mcp-seat guides propose`: a seat's matchup play_plan / notes edit and its
NOTES.md become regenerated guides pushed straight to mtg-decks main, never
forced, after the in-pod gates. There is no branch and no review step after
it: a guide edit is live the moment this returns `admitted`.

When origin main moved under the edit, the edit is replayed onto it with a
3-way merge. A clash is returned as `conflict` with git's markers written into
the working tree and no rebase or merge state left behind; the agent resolves
the markers and runs the same command again, and every gate runs again.

Runs against a tmp bare repo (`guides_git_testkit`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from guides_git_testkit import (
    GAME,
    GAMEPLAN,
    OLD_PLAN,
    OTHER,
    OTHER_PLAN,
    PLAN,
    RUN_NEW,
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
from playscript_testkit import SLUG

from magic_cards_model_api.playscripts.content_gate import CONTENT_REFUSED

NOTES = f"decks/{SLUG}/NOTES.md"
OTHER_NOTES = f"decks/{OTHER}/NOTES.md"
OTHER_GAMEPLAN = f"decks/{OTHER}/GAMEPLAN_GUIDE.md"
STUB = "izzet-prowess"
STUB_NOTES = f"decks/{STUB}/NOTES.md"
WIKI = f"decks/{SLUG}/analysis/{RUN_NEW}"

SEAT_ROW = {
    "name": "eldrazi tron",
    "origin": "seat",
    "play_plan": {
        "keep": "hands with a one-drop and Bolt",
        "goldfish_for": "a turn four kill before Karn lands",
        "watch_for": "Chalice of the Void on one",
        "posture": "race",
        "posture_why": "their late game beats ours, so the clock is the plan",
    },
    "notes": ["Bolt the Matter Reshaper only when it blocks."],
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    return isolate_git(tmp_path, monkeypatch)


@pytest.fixture
def render_log(tmp_path, monkeypatch):
    log = tmp_path / "render.log"
    monkeypatch.setenv("MTG_TEST_RENDER_LOG", str(log))
    return log


@pytest.fixture
def sidecars_log(tmp_path, monkeypatch):
    log = tmp_path / "sidecars.log"
    monkeypatch.setenv("MTG_TEST_SIDECARS_LOG", str(log))
    return log


@pytest.fixture
def bare(tmp_path, isolated):
    return seed_guides(tmp_path, extra_deck=True)


@pytest.fixture
def root(tmp_path, bare):
    return clone(bare, tmp_path / "guides")


def propose(capsys, root: Path, *decks: str):
    flags = [arg for deck in (decks or (SLUG,)) for arg in ("--deck", deck)]
    return cli(capsys, "guides", "propose", *flags, "--game-id", GAME, "--root", str(root))


def edit_plan(root: Path, change, plan: str = PLAN) -> None:
    doc = json.loads((root / plan).read_text(encoding="utf-8"))
    change(doc)
    (root / plan).write_text(dump(doc), encoding="utf-8")


def good_edit(doc) -> None:
    doc["matchups"][0]["play_plan"]["our_tactic"] = "go under the frog"
    doc["matchups"][0]["notes"].append("Hold Bolt for the second frog.")
    doc["matchups"][1]["notes"] = ["Board in all three."]


def logged(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def status(root: Path) -> list[str]:
    """The changed paths, staged or not."""
    out = git(root, "status", "--porcelain", "--untracked-files=all")
    return sorted(line[3:] if line[2:3] == " " else line[2:] for line in out.splitlines())


def untouched(root: Path, bare: Path, before: str) -> None:
    """Nothing reached origin and the clone is still on the same main."""
    assert main_sha(bare) == before
    assert branches(bare) == {"main"}
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(root, "rev-parse", "HEAD") == before


def no_merge_state(root: Path) -> None:
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        assert not (root / ".git" / name).exists(), name


# -- the happy path: straight to main ---------------------------------------------------------


def test_an_edit_is_rendered_committed_and_pushed_to_main(
    capsys, monkeypatch, root, bare, render_log, sidecars_log
):
    monkeypatch.setenv("MTG_ROLE", "advisor")
    calls = interpose(monkeypatch)
    edit_plan(root, good_edit)
    (root / NOTES).write_text("# notes\n- the frog is a race\n", encoding="utf-8")

    rc, result, out, err = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert result["sha"] == main_sha(bare) == git(root, "rev-parse", "HEAD")
    assert "pr_url" not in result and "branch" not in result
    assert branches(bare) == {"main"}

    assert logged(render_log) == [["write", [f"decks/{SLUG}"]], ["check", [f"decks/{SLUG}"]]]
    assert logged(sidecars_log) == [[3, WIKI]]
    changed = git(bare, "diff", "--name-only", "main~1", "main").splitlines()
    assert sorted(changed) == sorted([PLAN, NOTES, GAMEPLAN])
    guide = git(bare, "show", f"main:{GAMEPLAN}")
    assert "go under the frog" in guide and "Board in all three." in guide
    assert git(bare, "log", "-1", "--format=%an|%ae|%s", "main") == (
        f"mtg-buddy {SLUG}|mtg-buddy@mtg-trainer.invalid|Guides: {SLUG} after game {GAME}"
    )
    assert trailers(bare, "main", "Game-Id") == [GAME]
    assert status(root) == []
    no_merge_state(root)

    # Never forced, never a branch, never the GitHub REST API.
    pushes = [c["argv"] for c in calls if c["argv"][:2] == ["git", "push"]]
    assert pushes == [["git", "push", "origin", "HEAD:refs/heads/main"]]
    assert TOKEN not in out + err
    assert all(TOKEN not in " ".join(c["argv"]) for c in calls)
    assert all(TOKEN not in json.dumps(c["env"]) for c in calls)
    assert TOKEN not in (root / ".git" / "config").read_text(encoding="utf-8")


def test_notes_md_alone_is_admitted_without_the_sidecar_gate(
    capsys, root, bare, render_log, sidecars_log
):
    (root / NOTES).write_text("# notes\n- new\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert git(bare, "diff", "--name-only", "main~1", "main").splitlines() == [NOTES]
    assert logged(sidecars_log) == []


def test_nothing_to_propose(capsys, root, bare, render_log):
    before = main_sha(bare)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit")
    untouched(root, bare, before)


def test_a_local_commit_not_on_origin_main_is_refused(capsys, root, bare, render_log):
    (root / NOTES).write_text("# notes\n- committed by hand\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "by hand")
    head = git(root, "rev-parse", "HEAD")
    before = main_sha(bare)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert "a local commit not on origin/main" in result["message"]
    assert git(root, "rev-parse", "HEAD") == head
    assert main_sha(bare) == before


# -- (a) several decks in one call ---------------------------------------------------------


def test_two_decks_are_one_commit_rendered_together(capsys, root, bare, render_log, sidecars_log):
    edit_plan(root, good_edit)
    edit_plan(root, good_edit, OTHER_PLAN)
    (root / OTHER_NOTES).write_text("# frog notes\n- they race well\n", encoding="utf-8")

    rc, result, _, _ = propose(capsys, root, SLUG, OTHER, SLUG)
    assert (rc, result["result"]) == (0, "admitted"), result
    decks = [f"decks/{SLUG}", f"decks/{OTHER}"]
    assert logged(render_log) == [["write", decks], ["check", decks]]
    assert sorted(w for _, w in logged(sidecars_log)) == sorted(
        [WIKI, str(Path(OTHER_PLAN).parent.parent.as_posix())]
    )
    assert git(bare, "rev-list", "--count", "main~1..main") == "1"
    assert git(bare, "log", "-1", "--format=%an|%s", "main") == (
        f"mtg-seat {SLUG}|Guides: {SLUG}, {OTHER} after game {GAME}"
    )
    changed = git(bare, "diff", "--name-only", "main~1", "main").splitlines()
    assert sorted(changed) == sorted([PLAN, GAMEPLAN, OTHER_PLAN, OTHER_NOTES, OTHER_GAMEPLAN])


def test_a_change_under_a_deck_not_listed_is_refused_naming_the_missing_deck(
    capsys, root, bare, render_log
):
    before = main_sha(bare)
    edit_plan(root, good_edit)
    (root / OTHER_NOTES).write_text("# frog notes\n- sneaked in\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert f"--deck {OTHER}" in result["message"]
    assert result["files"] == [OTHER_NOTES]
    assert logged(render_log) == []
    untouched(root, bare, before)
    assert status(root) == sorted([OTHER_NOTES, PLAN])


def test_every_deck_name_is_checked(capsys, root, bare):
    (root / NOTES).write_text("# notes\n- new\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root, SLUG, "../main")
    assert (rc, result["result"]) == (1, "refused"), result
    assert "../main" in result["message"]


# -- (b) stub decks ---------------------------------------------------------------------------


def test_a_new_stub_deck_is_created_and_never_rendered(
    capsys, root, bare, render_log, sidecars_log
):
    (root / STUB_NOTES).parent.mkdir(parents=True)
    (root / STUB_NOTES).write_text(
        "# izzet prowess\n- they chain cantrips into a big prowess turn\n", encoding="utf-8"
    )
    rc, result, _, _ = propose(capsys, root, STUB)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert logged(render_log) == []
    assert logged(sidecars_log) == []
    assert git(bare, "diff", "--name-only", "main~1", "main").splitlines() == [STUB_NOTES]
    assert git(bare, "ls-tree", "-r", "--name-only", "main", f"decks/{STUB}") == STUB_NOTES

    # An existing stub takes further NOTES.md edits, beside a guided deck's.
    with (root / STUB_NOTES).open("a", encoding="utf-8") as handle:
        handle.write("- Bolt the Swiftspear on sight\n")
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root, SLUG, STUB)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert logged(render_log) == [["write", [f"decks/{SLUG}"]], ["check", [f"decks/{SLUG}"]]]


@pytest.mark.parametrize("name", ["decklist.txt", "archetype.json", "GAMEPLAN_GUIDE.md"])
def test_a_stub_never_gets_any_other_file(capsys, root, bare, render_log, name):
    before = main_sha(bare)
    (root / STUB_NOTES).parent.mkdir(parents=True)
    (root / STUB_NOTES).write_text("# izzet prowess\n- tempo\n", encoding="utf-8")
    (root / f"decks/{STUB}/{name}").write_text("4 Lightning Bolt\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root, STUB)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["files"] == [f"decks/{STUB}/{name}"]
    untouched(root, bare, before)


def _rename_archetype(bare: Path, tmp_path: Path, name: str) -> None:
    other = clone(bare, tmp_path / "renamer")
    (other / f"decks/{SLUG}/archetype.json").write_text(dump({"archetype": name}), "utf-8")
    git(other, "commit", "-q", "-am", "rename the archetype")
    git(other, "push", "-q", "origin", "HEAD:main")


def test_a_new_stub_colliding_with_a_guided_deck_is_refused(
    capsys, tmp_path, root, bare, render_log
):
    _rename_archetype(bare, tmp_path, "Boros Energy")
    git(root, "pull", "-q", "origin", "main")
    before = main_sha(bare)
    (root / "decks/boros-energy").mkdir()
    (root / "decks/boros-energy/NOTES.md").write_text("# boros\n- fast\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root, "boros-energy")
    assert (rc, result["result"]) == (1, "refused"), result
    assert SLUG in result["message"]
    assert logged(render_log) == []
    untouched(root, bare, before)


# -- (c) the boarding plan's fields --------------------------------------------------------


def _set(path, value):
    def change(doc):
        *parents, leaf = path
        target = doc
        for key in parents:
            target = target[key]
        target[leaf] = value

    return change


FORBIDDEN_KEYS = [
    ("matchups[0].play", _set(("matchups", 0, "play"), {"in": [], "out": []})),
    ("matchups[0].stance", _set(("matchups", 0, "stance"), "hate")),
    ("matchups[0].extra", _set(("matchups", 0, "extra"), "new key")),
    ("gaps", _set(("gaps",), [])),
    ("matchups[1]", lambda doc: doc["matchups"].pop()),
    ("matchups[0].name", lambda doc: doc["matchups"].reverse()),
]


@pytest.mark.parametrize(("where", "change"), FORBIDDEN_KEYS, ids=[f[0] for f in FORBIDDEN_KEYS])
def test_a_forbidden_boarding_plan_key_is_refused(capsys, root, bare, render_log, where, change):
    before = main_sha(bare)
    edit_plan(root, change)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    # T38's content gate did not take this refusal over: the remedy for a key
    # outside the allowlist is to revert it, not to reword it.
    assert result["result"] != CONTENT_REFUSED
    assert where in result["message"]
    assert logged(render_log) == []
    untouched(root, bare, before)


def test_an_appended_seat_row_is_admitted(capsys, root, bare, render_log, sidecars_log):
    edit_plan(root, lambda doc: doc["matchups"].append(SEAT_ROW))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    landed = json.loads(git(bare, "show", f"main:{PLAN}"))
    assert landed["matchups"][-1] == SEAT_ROW
    assert "eldrazi tron" in git(bare, "show", f"main:{GAMEPLAN}")
    assert logged(sidecars_log) == [[3, WIKI]]


def _row(**changes):
    row = json.loads(json.dumps(SEAT_ROW))
    for key, value in changes.items():
        if value is None:
            row.pop(key)
        else:
            row[key] = value
    return row


BAD_ROWS = [
    ("matchups[2].origin", _row(origin=None)),
    ("matchups[2].origin", _row(origin="research")),
    ("matchups[2].stance", _row(stance="race")),
    ("matchups[2].name", _row(name="")),
    ("matchups[2].name", _row(name="Dimir  Frog")),
    ("matchups[2].play_plan.watch_for", _row(play_plan={**SEAT_ROW["play_plan"], "watch_for": 3})),
    (
        "matchups[2].play_plan.keep",
        _row(play_plan={k: v for k, v in SEAT_ROW["play_plan"].items() if k != "keep"}),
    ),
    ("matchups[2].play_plan.extra", _row(play_plan={**SEAT_ROW["play_plan"], "extra": "x"})),
    ("matchups[2].notes", _row(notes="not a list")),
    ("matchups[2].notes", _row(notes=None)),
    ("matchups[2]", "not an object"),
]


@pytest.mark.parametrize(
    ("where", "row"), BAD_ROWS, ids=[f"{w}-{i}" for i, (w, _) in enumerate(BAD_ROWS)]
)
def test_a_bad_appended_row_is_refused_with_its_json_path(
    capsys, root, bare, render_log, where, row
):
    before = main_sha(bare)
    edit_plan(root, lambda doc: doc["matchups"].append(row))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert where in result["message"], result["message"]
    assert logged(render_log) == []
    untouched(root, bare, before)


def test_two_appended_rows_with_one_name_are_refused(capsys, root, bare, render_log):
    second = _row(name="Eldrazi Tron")
    edit_plan(root, lambda doc: doc["matchups"].extend([SEAT_ROW, second]))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert "matchups[3].name" in result["message"]


def test_a_system_problem_in_an_appended_row_is_refused_content(capsys, root, bare, render_log):
    row = _row(play_plan={**SEAT_ROW["play_plan"], "watch_for": "the api returned a 422"})
    edit_plan(root, lambda doc: doc["matchups"].append(row))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, CONTENT_REFUSED), result


FORBIDDEN_FILES = [
    f"decks/{SLUG}/decklist.txt",
    f"decks/{SLUG}/evidence/run.json",
    f"decks/{SLUG}/sim-profiles.json",
    f"decks/{SLUG}/evidence-spec.json",
    f"decks/{SLUG}/shell.json",
    f"decks/{SLUG}/archetype.json",
    f"decks/{SLUG}/plays/fetch-untapped-shock.json",
    GAMEPLAN,
    OLD_PLAN,
    "meta/card-data.json",
    "scripts/render_guides.py",
    "new-file.txt",
]


@pytest.mark.parametrize("path", FORBIDDEN_FILES)
def test_a_forbidden_file_is_refused(capsys, root, bare, render_log, path):
    before = main_sha(bare)
    edit_plan(root, good_edit)
    (root / path).write_text("changed\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["result"] != CONTENT_REFUSED
    assert path in result["files"]
    assert logged(render_log) == []
    untouched(root, bare, before)


def test_an_unrelated_plays_edit_is_refused_before_anything_is_reset(
    capsys, tmp_path, root, bare, render_log
):
    """`reset --hard` is only safe because the allowlist refuses everything
    else first: with origin main moved and a play edited beside the guide
    edit, the play edit must survive untouched."""
    compete(bare, tmp_path / "competitor", "upstream moved")
    play = root / f"decks/{SLUG}/plays/fetch-untapped-shock.json"
    play.write_text(play.read_text(encoding="utf-8").replace('"priority": 60', '"priority": 61'))
    edited = play.read_text(encoding="utf-8")
    (root / NOTES).write_text("# notes\n- mine\n", encoding="utf-8")
    head = git(root, "rev-parse", "HEAD")

    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert play.read_text(encoding="utf-8") == edited
    assert (root / NOTES).read_text(encoding="utf-8") == "# notes\n- mine\n"
    assert git(root, "rev-parse", "HEAD") == head


def test_the_newest_run_is_the_one_allowed(capsys, root, bare, render_log):
    # The older run's plan is a different path, refused even for play_plan.
    doc = json.loads((root / OLD_PLAN).read_text(encoding="utf-8"))
    doc["matchups"][0]["play_plan"]["our_tactic"] = "stale"
    (root / OLD_PLAN).write_text(dump(doc), encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert result["result"] == "refused"
    assert result["files"] == [OLD_PLAN]
    assert branches(bare) == {"main"}


def test_a_new_run_directory_does_not_become_the_editable_plan(capsys, root, bare, render_log):
    """The newest plan is read off HEAD, so a run the agent adds is not it."""
    newer = PLAN.replace(RUN_NEW, f"{SLUG}-modern-2026-12-31")
    (root / newer).parent.mkdir(parents=True)
    (root / newer).write_text((root / PLAN).read_text(encoding="utf-8"), encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["files"] == [newer]


# -- (d) conflict markers -------------------------------------------------------------------

MARKED = "# notes\n<<<<<<< yours\n- mine\n=======\n- theirs\n>>>>>>> origin/main\n"


def test_conflict_markers_in_notes_are_refused(capsys, root, bare, render_log):
    before = main_sha(bare)
    (root / NOTES).write_text(MARKED, encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["files"] == [NOTES]
    assert "marker" in result["message"]
    untouched(root, bare, before)


def test_conflict_markers_in_the_plan_are_refused_before_it_is_parsed(capsys, root, bare):
    text = (root / PLAN).read_text(encoding="utf-8")
    marked = text.replace(
        '"our_tactic": "race them"',
        '<<<<<<< yours\n"our_tactic": "a"\n=======\n"our_tactic": "b"\n>>>>>>> origin/main',
    )
    (root / PLAN).write_text(marked, encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert result["files"] == [PLAN]
    assert "marker" in result["message"]
    assert "JSON" not in result["message"]


def test_a_line_merely_mentioning_equals_signs_is_not_a_marker(capsys, root, bare, render_log):
    (root / NOTES).write_text("# notes\n- ======= is not a marker here\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result


# -- (e) origin main moved: merge, conflict, retries -------------------------------------------


def _edit_mulligans(root: Path, text: str) -> None:
    notes = root / NOTES
    notes.write_text(
        notes.read_text(encoding="utf-8").replace("- keep a two-lander with Bolt", text),
        encoding="utf-8",
    )


def test_an_upstream_edit_to_another_part_of_the_file_merges_cleanly(
    capsys, tmp_path, root, bare, render_log
):
    compete(bare, tmp_path / "competitor", "upstream matchup line")
    _edit_mulligans(root, "- keep a two-lander only on the play")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    landed = git(bare, "show", f"main:{NOTES}")
    assert "- keep a two-lander only on the play" in landed
    assert "- upstream matchup line" in landed
    assert git(bare, "log", "-1", "--format=%s", "main~1") == "competitor upstream matchup line"
    assert status(root) == []
    no_merge_state(root)


def test_a_race_on_the_push_is_retried_and_merged(capsys, monkeypatch, tmp_path, root, bare):
    pushes: list[list[str]] = []

    def race(argv):
        if is_main_push(argv):
            pushes.append(argv)
            if len(pushes) == 1:
                compete(bare, tmp_path / "competitor", "first")

    interpose(monkeypatch, race)
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "admitted"), result
    assert len(pushes) == 2
    assert git(bare, "log", "-1", "--format=%s", "main~1") == "competitor first"
    assert git(bare, "rev-list", "--count", "main~1..main") == "1"
    assert "go under the frog" in git(bare, "show", f"main:{GAMEPLAN}")
    assert status(root) == []


def test_the_same_lines_changed_upstream_are_a_conflict_the_agent_resolves(
    capsys, tmp_path, root, bare, render_log
):
    old_head = git(root, "rev-parse", "HEAD")
    theirs = (
        (root / NOTES)
        .read_text(encoding="utf-8")
        .replace("- keep a two-lander with Bolt", "- mulligan every two-lander")
    )
    compete(bare, tmp_path / "competitor", "theirs", notes=theirs)
    upstream = main_sha(bare)
    _edit_mulligans(root, "- keep a two-lander on the draw")

    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "conflict"), result
    assert result["files"] == [NOTES]
    assert (result["base_sha"], result["upstream_sha"]) == (old_head, upstream)
    assert "same command" in result["message"]
    assert main_sha(bare) == upstream
    # The clone is on the new upstream, the markers are in the tree for the
    # agent, and git holds no rebase or merge state.
    assert git(root, "rev-parse", "HEAD") == upstream
    no_merge_state(root)
    marked = (root / NOTES).read_text(encoding="utf-8")
    assert "<<<<<<< yours" in marked and ">>>>>>> origin/main" in marked
    assert "- keep a two-lander on the draw" in marked and "- mulligan every two-lander" in marked
    assert logged(render_log) == []

    # Unresolved, the same command refuses on the markers.
    rc, again, _, _ = propose(capsys, root)
    assert (rc, again["result"]) == (1, "refused"), again

    resolved = theirs.replace(
        "- mulligan every two-lander", "- mulligan a two-lander on the play, keep it on the draw"
    )
    (root / NOTES).write_text(resolved, encoding="utf-8")
    rc, done, _, _ = propose(capsys, root)
    assert (rc, done["result"]) == (0, "admitted"), done
    assert git(bare, "show", f"main:{NOTES}") + "\n" == resolved
    assert git(bare, "rev-parse", "main~1") == upstream


def test_a_plan_conflict_leaves_markers_the_gates_refuse(capsys, tmp_path, root, bare):
    other = clone(bare, tmp_path / "planner")
    edit_plan(other, _set(("matchups", 0, "play_plan", "our_tactic"), "their tactic"))
    git(other, "commit", "-q", "-am", "their tactic")
    git(other, "push", "-q", "origin", "HEAD:main")
    edit_plan(root, _set(("matchups", 0, "play_plan", "our_tactic"), "my tactic"))

    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "conflict"), result
    assert result["files"] == [PLAN]
    marked = (root / PLAN).read_text(encoding="utf-8")
    assert "<<<<<<< yours" in marked and "my tactic" in marked and "their tactic" in marked
    no_merge_state(root)


def test_three_races_are_a_retryable_error_with_the_edit_left_in_the_tree(
    capsys, monkeypatch, tmp_path, root, bare, render_log
):
    pushes: list[list[str]] = []

    def race(argv):
        if is_main_push(argv):
            pushes.append(argv)
            compete(bare, tmp_path / "competitor", f"race {len(pushes)}")

    interpose(monkeypatch, race)
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "error"), result
    assert result["retryable"] is True
    assert len(pushes) == 3
    assert git(bare, "log", "-1", "--format=%s", "main") == "competitor race 3"
    assert branches(bare) == {"main"}
    # The edit is uncommitted on the newest upstream it fetched; the rendered
    # guide is put back.
    assert git(root, "rev-parse", "HEAD") == git(bare, "rev-parse", "main~1")
    assert status(root) == [PLAN]
    no_merge_state(root)


# -- the gates -------------------------------------------------------------------------------


def test_a_render_check_failure_is_gate_failed_and_leaves_the_edit(
    capsys, monkeypatch, root, bare, render_log
):
    before = main_sha(bare)
    monkeypatch.setenv("MTG_TEST_RENDER_CHECK_FAIL", "1")
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    untouched(root, bare, before)
    # The agent's edit stays for it to fix; the half-rendered guide does not.
    assert status(root) == [PLAN]


def test_a_missing_renderer_is_gate_failed(capsys, tmp_path, isolated):
    bare = seed_guides(tmp_path, render_guides=False)
    root = clone(bare, tmp_path / "guides")
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert "render_guides.py" in result["message"]
    assert branches(bare) == {"main"}


def test_a_verify_sidecars_failure_is_gate_failed(capsys, monkeypatch, root, bare, render_log):
    before = main_sha(bare)
    monkeypatch.setenv("MTG_TEST_SIDECARS_FAIL", "1")
    edit_plan(root, lambda doc: doc["matchups"].append(SEAT_ROW))
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert "verify_sidecars" in result["message"]
    assert "not in the boarding field" in result["output"]
    untouched(root, bare, before)
    assert status(root) == [PLAN]


def test_a_missing_verify_sidecars_fails_closed(capsys, tmp_path, isolated):
    bare = seed_guides(tmp_path, verify_sidecars=False)
    before = main_sha(bare)
    root = clone(bare, tmp_path / "guides")
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert "verify_sidecars.py" in result["message"]
    assert main_sha(bare) == before


# -- roles -------------------------------------------------------------------------------------


def test_a_seat_and_a_buddy_of_one_game_both_land_on_main(
    capsys, monkeypatch, tmp_path, bare, render_log
):
    seat_clone = clone(bare, tmp_path / "seat-guides")
    buddy_clone = clone(bare, tmp_path / "buddy-guides")

    monkeypatch.setenv("MTG_ROLE", "seat")
    edit_plan(seat_clone, good_edit)
    rc, seat, _, _ = propose(capsys, seat_clone)
    assert (rc, seat["result"]) == (0, "admitted"), seat

    monkeypatch.setenv("MTG_ROLE", "advisor")
    _edit_mulligans(buddy_clone, "- the buddy keeps a two-lander")
    rc, buddy, _, _ = propose(capsys, buddy_clone)
    assert (rc, buddy["result"]) == (0, "admitted"), buddy

    assert branches(bare) == {"main"}
    log = git(bare, "log", "--format=%an", "-2", "main").splitlines()
    assert log == [f"mtg-buddy {SLUG}", f"mtg-seat {SLUG}"]


# -- T14 (defect D-1): the /guides checkout is entered by one writer at a time --


def test_there_is_no_guides_branch_any_more():
    from magic_cards_model_api.playscripts import guides_propose

    assert not hasattr(guides_propose, "guides_branch")


def test_the_reviewer_role_is_accepted_by_the_container_role_read(monkeypatch):
    from magic_cards_model_api.playscripts.gitwork import REVIEWER_ROLE, identity, role

    monkeypatch.setenv("MTG_ROLE", REVIEWER_ROLE)
    assert role() == REVIEWER_ROLE
    name, email = identity(SLUG)
    assert name.startswith("mtg-reviewer")
    assert email == "mtg-reviewer@mtg-trainer.invalid"


def test_an_unknown_role_is_still_refused(monkeypatch):
    from magic_cards_model_api.playscripts.gitwork import Outcome, role

    monkeypatch.setenv("MTG_ROLE", "coach")
    with pytest.raises(Outcome):
        role()


def test_the_checkout_lock_serializes_two_writers(tmp_path):
    """Clause (d): with one writer in its git phase, the other waits and is
    ordered after it - never interleaved. The writers here stand in for the
    review's own tool call and the handover turn's `plays admit`; what is
    being proved is the lock, not either command's git work."""
    import threading
    import time

    from magic_cards_model_api.playscripts.gitwork import checkout_lock

    root = tmp_path / "guides"
    root.mkdir()
    order: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()

    def reviewer() -> None:
        with checkout_lock(root):
            order.append("reviewer in")
            first_inside.set()
            release_first.wait(5.0)
            order.append("reviewer out")

    def handover() -> None:
        first_inside.wait(5.0)
        with checkout_lock(root):
            order.append("handover in")
            order.append("handover out")

    threads = [threading.Thread(target=reviewer), threading.Thread(target=handover)]
    for thread in threads:
        thread.start()
    first_inside.wait(5.0)
    # The second writer has had its chance and must still be outside.
    time.sleep(0.2)
    assert order == ["reviewer in"]
    release_first.set()
    for thread in threads:
        thread.join(5.0)
    assert order == ["reviewer in", "reviewer out", "handover in", "handover out"]


def test_the_checkout_lock_is_held_across_processes(tmp_path):
    """An in-process `threading.Lock` would not do: the reviewer's own tool
    call and `mcp-seat plays admit` are separate processes in one container,
    which is exactly the shape that corrupts a fetch-and-rebase."""
    import subprocess
    import sys
    import threading

    from magic_cards_model_api.playscripts.gitwork import checkout_lock

    root = tmp_path / "guides"
    root.mkdir()
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path('src').resolve())!r})\n"
        "from magic_cards_model_api.playscripts.gitwork import checkout_lock\n"
        "start = time.monotonic()\n"
        f"with checkout_lock({str(root)!r}):\n"
        "    print(round(time.monotonic() - start, 2))\n"
    )
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with checkout_lock(root):
            held.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5.0)
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.poll() is None
        release.set()
        out, _ = child.communicate(timeout=10)
        assert child.returncode == 0, out
        assert float(out.strip()) >= 0.0
    finally:
        release.set()
        holder.join(5.0)


def test_propose_takes_the_checkout_lock(capsys, root, bare, render_log, monkeypatch):
    """`guides propose` does its git work under the lock, so the reviewer's
    own write channel cannot be inside the same checkout at the same time."""
    from magic_cards_model_api.playscripts import gitwork

    taken: list[str] = []
    real = gitwork.checkout_lock

    def watched(where, timeout: float | None = None):
        taken.append(str(where))
        return real(where, timeout)

    monkeypatch.setattr(gitwork, "checkout_lock", watched)
    edit_plan(root, good_edit)
    rc, body, _, _ = propose(capsys, root)
    assert (rc, body["result"]) == (0, "admitted"), body
    assert taken == [str(root.resolve())]


def test_the_checkout_lock_gives_up_rather_than_waiting_for_ever(tmp_path):
    """A wedged holder must not hang a handover turn inside its time budget:
    the wait is bounded and the refusal names the checkout."""
    import threading

    from magic_cards_model_api.playscripts.gitwork import Outcome, checkout_lock

    root = tmp_path / "guides"
    root.mkdir()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with checkout_lock(root):
            held.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5.0)
    try:
        with pytest.raises(Outcome) as refusal, checkout_lock(root, timeout=0.2):
            pass
        assert refusal.value.result["result"] == "error"
        assert "guides" in refusal.value.result["message"]
    finally:
        release.set()
        holder.join(5.0)


def test_a_handover_write_waits_for_a_review_in_flight_and_never_interleaves(
    capsys, root, bare, render_log
):
    """Clause (d), at the command level rather than the primitive's: with the
    reviewer's own write channel inside the checkout, the handover turn's
    `guides propose` does not start its git phase - it waits, and runs
    afterwards, with nothing of the two overlapping."""
    import threading
    import time

    from magic_cards_model_api.playscripts.gitwork import checkout_lock

    order: list[str] = []
    reviewer_in = threading.Event()
    release = threading.Event()
    outcome: list[dict] = []

    def reviewer() -> None:
        with checkout_lock(root):
            order.append("review in")
            reviewer_in.set()
            release.wait(10.0)
            order.append("review out")

    def handover() -> None:
        reviewer_in.wait(10.0)
        order.append("handover asked")
        rc, body, _, _ = propose(capsys, root)
        order.append("handover done")
        outcome.append({"rc": rc, **body})

    edit_plan(root, good_edit)
    threads = [threading.Thread(target=reviewer), threading.Thread(target=handover)]
    for thread in threads:
        thread.start()
    reviewer_in.wait(10.0)
    time.sleep(0.3)
    assert order == ["review in", "handover asked"], order
    release.set()
    for thread in threads:
        thread.join(30.0)

    assert order == ["review in", "handover asked", "review out", "handover done"], order
    assert outcome and outcome[0]["result"] == "admitted", outcome
