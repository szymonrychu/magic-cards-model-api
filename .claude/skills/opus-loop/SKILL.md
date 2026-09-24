---
name: opus-loop
description: Use when running the Opus-vs-Opus self-play loop to grow the play-script DSL and find DSL/UX gaps in the seat MCP surface - not when hunting engine rules bugs. Covers docker compose bring-up, the orchestrator/overseer split, watch_game's bounded wait, and batching dsl/ux findings into fix workflows.
---

# Opus-vs-Opus DSL/UX loop

## Purpose - read this before reaching for `playtest-loop`

This skill is **distinct from `playtest-loop`** (both the workbench copy and
`code/magic-cards-model`'s own). `playtest-loop` hunts **engine** rules bugs:
two Sonnet seats over `serve.py --match`, stop on the first divergence from
the Comprehensive Rules, adjudicate, fix the engine, merge.

This loop does not hunt engine bugs. Its whole point is **coverage of the
play-script DSL and the seat MCP surface**: two Opus seats play real games
against each other, and every decision one of them had to make by hand - a
DSL play that does not exist yet, or an MCP call sequence that took more
calls than it should have - is the signal. Per the 2026-09-24 gate-1
decision: **the DSL should eventually replace agents; agents act manually
only rarely, and play count must grow dramatically.** An engine bug found
along the way still stops the game and routes to `engineering:debug` in the
engine repo (`code/magic-cards-model`) - that path is not disabled, it is
just not what this loop is *for*.

## Bring-up

```sh
cp .env.example .env   # then set ONE of:
                        #   CLAUDE_CODE_OAUTH_TOKEN   (from `claude setup-token`)
                        #   ANTHROPIC_API_KEY         (a real sk-ant-... key)
                        # and MTG_DECKS (a local mtg-decks checkout) + MTG_DECKS_GH_PAT
mise run compose:aivai
```

This is `docker-compose.aivai.yml` (DR7), a **separate** compose file from
`docker-compose.yml` - not a profile of it. It duplicates the single-seat
stack into two of everything a wrapper-mode AI seat needs: `agent-p1`/
`agent-p2` (each its own writable `mtg-decks` checkout via `guides-init-p1`/
`-p2`, so two seats committing guide edits never share one git index) and
`driver-p1`/`driver-p2` (each pointed at its own agent, never the other
seat's). One `game` service lists both drivers in `DRIVER_URLS`, and
`ai_seat.driver_urls()`/`_dispatch_to_driver` routes each seat's dispatch to
whichever driver is idle - this is what makes AIvAI work in compose at all;
`docker-compose.yml`'s single `DRIVER_URL` 409s a second seat's dispatch
("one container, one match").

Bring it down with `mise run compose:aivai:down` (pass `-v` to also drop the
postgres and cache volumes).

**Seats are Opus at low effort** (`MODEL=claude-opus-5`, `EFFORT=low` on both
`agent-p1`/`agent-p2` - the compose file's own default, cheap enough for a
fast self-play loop). **The post-game review is Opus at high effort**
regardless - `agent_settings.review_default()` is fixed server-side and never
reads the compose file's env. Do not raise the seats' effort to make them
more reliable; see "Stalled seats" below for the loop's actual mitigation.

## Starting a match

The orchestrator starts each game itself, from the lobby, once compose is
healthy:

```
POST http://127.0.0.1:8000/api/lobby/games
{
  "players": [
    {"username": "p1", "decklist_text": "<a decklist from mtg-decks>", "ai": true},
    {"username": "p2", "decklist_text": "<a decklist from mtg-decks>", "ai": true}
  ]
}
```

Both players carry `"ai": true`. Decklists come from `mtg-decks` - pick two
real, currently-legal archetypes (Modern/Pioneer/Standard only, per this
project's target-format rule); a mirror is fine here, this loop is not
sampling matchup diversity the way `playtest-loop`'s goal condition does.
The response (`NewUiGameResponse`) carries `game_id` and `seat_uids`; nothing
further is needed to start play - `ai: true` is what gets each seat dispatched
to its driver.

## Roles

**Orchestrator** (you, unless told otherwise): starts games from the lobby,
owns every fix workflow, and is the only one that talks to
`workbench:start-development` / `workbench:plan-execute` or dispatches
`engineering:debug`. Never fixes a DSL/UX gap inline itself - that is what
the batched fix workflow (below) is for.

**Overseer subagent, one per game, `model: sonnet` set explicitly** (never
inherit the session default - this is a watch-and-report loop, not a design
decision, and Sonnet is the tractable model for it per the model-selection
table). Spawn one the moment a game starts; do not batch overseers behind
each other - one game watched is one overseer running, immediately.

## What the overseer does, per game

1. **Runs `scripts/watch_game.py`**, bounded: `--max-seconds` at most 600 (10
   minutes) per wait. When it returns `timeout`, the overseer does not idle -
   it re-enumerates (is the game still live? did a `plays_outcome`/`ai_review`
   row land while it was waiting? did the seats just go quiet?) and calls
   `watch_game.py` again. No open-ended hold, ever - this mirrors the
   standing "never report waiting" rule: a `timeout` edge is a signal to look
   harder, not a reason to say "still watching."

2. **Reads `findings` off every edge** `watch_game.py` returns
   (`report`/`game_over`/`stalled`/`timeout` all carry it - DR8). Each entry
   is `{"seq", "player", "category", "text"}` with `category` `dsl` or `ux`.
   These never stopped the game; they are exactly the coverage signal this
   loop exists to collect.

3. **Also reads UX friction directly off the ledger** (`GET
   /api/games/{id}/log`), independent of whether either seat filed a
   `report_problem`: a refused call, a retry of the same call in a row, a
   long chain of calls for what should be one decision, or an answer a seat
   visibly misread. File each as its own `ux` finding with the same shape
   `watch_game.py` uses, so it merges cleanly with the DSL/UX findings
   already collected.

4. **Treats a stalled low-effort seat as a finding, not a crash.**
   `agent_settings.py`'s own docstring names this risk: `low` effort is
   documented as stopping to ask when a task is multi-step, but an AI seat's
   `AskUserQuestion` is denied, so a seat that wants to ask and cannot may
   simply go quiet. `watch_game.py`'s `stalled` edge is exactly this
   watcher's catch for it. When it fires, the overseer restarts the stalled
   seat's driver assignment (the same recovery `ai_pregame.py`'s
   `GONE`/`activity()` backstop already uses for a seat that never started)
   and files one `ux` finding naming the stall - it is friction the DSL/UX
   loop should reduce, not an engine defect.

5. **Waits (bounded, same 10-minute rule) for the post-game sequence**: game
   over -> reflection -> `ai_review` (or `ai_review_failed`, best-effort and
   non-blocking - `consider_ai_review` in the driver) -> handover ->
   `plays_outcome` row. `watch_game.py`'s `game_over` edge already fires once
   the board is decided; the overseer keeps polling `GET .../log` after that
   for the `ai_review`/`plays_outcome` rows rather than treating `game_over`
   as the end of its job, since the DSL submissions this loop cares about
   (refined plays, new plays, guide updates) happen in the handover turn,
   after `game_over`.

6. **Reports, per game, once the handover has landed** (or the bound above
   has been re-enumerated to a concrete reason it has not):
   - **result**: winner (or drawn), turn count.
   - **play statistics**, per seat, straight from `GET .../review`'s
     `play_stats` (`watch_game.py`'s `game_over` edge already carries this
     when the overseer passed `--review-key`; pass the game's admin key so
     it is always reachable) - plays in the deck (`plays_total`), `added`,
     `updated`, `used`, `denied`.
   - **findings by category** - the `dsl`/`ux` rows collected in steps 2-4,
     grouped.
   - an **engine** report (see next section) is never folded into this
     summary - it already ended the game and its own routing.

## An engine report stops the game

`watch_game.py`'s `report` edge fires **only** for an `engine`-category
`report_problem` row (or a legacy row with no `category` at all, which
counts as `engine`) - never for `dsl`/`ux`. When it fires, the overseer:

- stops treating this game as live (it already stopped itself),
- hands the report straight to the orchestrator, which routes it to
  `engineering:debug` **in the engine repo** (`code/magic-cards-model`) -
  this loop never adjudicates or fixes an engine defect itself, exactly as
  `playtest-loop` already owns that path,
- does not batch this into a dsl/ux fix workflow; it is a different kind of
  defect with a different owner path.

## Batching dsl/ux findings into fix workflows

The overseer **never fixes a dsl/ux finding inline** - it only collects and
reports. The orchestrator batches findings into **one
`workbench:start-development` / `plan-execute` fix workflow per batch**, where
a batch is:

- every dsl/ux finding from **one game**, once that game's report (above) is
  in hand, **or**
- **once 5 findings have accumulated** across any number of in-flight games,
  whichever comes first - so a slow trickle from one long game does not sit
  unbatched for the whole loop, and a quiet run of clean games does not
  starve the DSL of the two or three findings it did produce.

A batch's fix workflow is where refinement actually happens: per the gate-1
decisions, a `declined` offer is refined (its conditions tightened to name
the corner case precisely - an agent may not turn down a play whose
conditions hold on corner-case grounds, it must describe the corner case as a
condition split instead) and a new per-decision play is drafted, generalized
to the mechanic, and merged/deduped against the existing corpus
(`play-conflict` exclusivity, the `<=1 new move-bearing play per review`
bar). **Every properly generalized decision goes into the guide** - this is
the existing "guides push to main" flow (`playscripts/guides_propose.py`,
`NOTES.md` + matchup boarding-plan allowlist, content-gated, pushed to
`mtg-decks` main with race retries): agents pushing guide edits to `mtg-decks`
main and fixing conflicts is standing practice here, not something this
skill introduces.

## No-waiting and bounded-wait, for this loop specifically

Every wait in this skill - `watch_game.py`'s own poll loop, the overseer's
wait for `ai_review`/`plays_outcome`, the orchestrator's wait on a batch fix
workflow - is bounded at 10 minutes unless the tool sets its own shorter cap,
exactly as the standing rule requires. "Still watching the game" and "still
waiting on the handover" are never reportable states on their own: re-enumerate
what is actually true (is the game still making log entries? has the driver's
own `/status` gone idle? did a sibling game's fix batch just land, unblocking
a `no-change` re-check?) and act on whatever that finds before reporting
anything as a wait. A background `watch_game.py` process running is not by
itself a reason for the overseer to be idle between its bounded polls.
