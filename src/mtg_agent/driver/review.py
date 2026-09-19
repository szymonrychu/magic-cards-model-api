"""The post-game AI review's own prompt: a pure function from a seat's own
reflection (the "handoff" - `prompt.reflection_turn`'s own account, posted
by `app.post_reflection`) plus the game's review record
(`GET /api/games/{id}/review`'s own body, `review.build_review`'s output) to
the text asked of Opus.

**Why this exists as a separate call from the reflection, on a separate
model.** The reflection is the SEAT's own words, written by whichever model
just played the game (Sonnet, since 2026-09-19's cost cut -
`agent_settings.DEFAULT_MODEL`) - it is testimony, not analysis, and
`review.py`'s own docstring is explicit that nothing in this codebase grades
a declaration or a piece of advice. The AI review is the one place that
changes: a second, independent pass, on the stronger and slower model
(`agent_settings.AgentSettings.review_default`, fixed to Opus at high
effort, deliberately never read from the play defaults) reading the same
material a human reviewer would - what the seat predicted, what actually
happened, and what it said about its own game afterward - and writing a
critique. It still grades nothing the engine did; it is an opinion about how
well the seat played, offered once, after the fact, the same way a human
coach's post-mortem would be.

**Where this runs.** Nowhere near the driver's own container: the driver
(`Dockerfile`, no `claude` binary) cannot invoke it directly. `app.py`'s
`post_ai_review` posts this prompt to the AGENT container's wrapper
(`POST /v1/review`, `mtg_agent.wrapper.api`), which runs a bounded, one-shot
`claude --print` subprocess (`wrapper.oneshot.run_review`) entirely separate
from the wrapper's supervised interactive session - see that module's own
docstring for why a second `claude` process, in the one container that has
the binary, is the right shape rather than another turn of the session that
just finished playing.

**Not deduplicated.** In an AIvAI game each seat's own driver reaches
`app.turn_done`'s reflection branch independently, and each could in
principle generate and file its own AI review. This is accepted rather than
coordinated against: `audit.AI_REVIEW_ACTION` carries no uniqueness
constraint (the same is already true of `audit.REFLECTION_ACTION`, which a
human seat and an AI seat could each file their own of), `ui/review_model.js`
reads the most recent row of a kind rather than assuming exactly one, and
the ordinary case this whole feature exists for - a human seat plus one AI
opponent - never has the race at all, since only the AI seat's driver ever
reaches this code path. Coordinating a single generation across two
independent per-seat containers, for a rare AIvAI-only extra API call that
costs nothing to simply allow twice, was judged not worth the cross-pod
locking it would take."""

from __future__ import annotations

from typing import Any

from .prompt import Assignment

#: How many of a game's declarations the prompt carries in full. A long game
#: can declare a plan every turn for 20+ turns; `review.py`'s own
#: `_declarations` already strips each one down to plan/prediction/did/
#: opponent_did (no board snapshot), so even the full list is modest, but a
#: cap keeps the prompt bounded for the rare very long game rather than
#: growing it without limit. The newest declarations are the ones nearest
#: the result and so the most useful to a reviewer - kept, not the oldest.
MAX_DECLARATIONS = 30


def review_prompt(
    assignment: Assignment,
    reflection_text: str,
    review_record: dict[str, Any],
) -> str:
    """The text asked of Opus: `assignment.player`'s own reflection, then the
    game's declarations (plan vs. what actually happened) and any advice
    exchanged, then the ask. Never the raw `review_record["entries"]` - those
    carry a full board snapshot per row (`review.py`'s own docstring: "about
    1.5 kB each") for a purpose this prompt has no use for; the
    already-paired `declarations`/`advice` are the same material a human
    reviewer reads on the review page."""
    lines: list[str] = []
    players = review_record.get("players") or [assignment.player, assignment.opponent]
    winner = review_record.get("winner")
    drawn = review_record.get("drawn")
    if drawn:
        outcome = "the game was a draw"
    elif winner:
        outcome = f"{winner} won"
    else:
        outcome = "the outcome was not recorded"
    lines.append(
        f"You are reviewing one game of Magic: the Gathering between "
        f"{' and '.join(players)}. {outcome}. You are writing about "
        f"{assignment.player}'s play specifically."
    )
    lines.append("")
    lines.append(f"{assignment.player}'s own reflection, written just after the game:")
    lines.append(reflection_text.strip() or "(the seat left no reflection)")
    lines.append("")

    declarations = review_record.get("declarations") or []
    if declarations:
        lines.append(
            f"{assignment.player}'s declared plans this game (prediction, then what "
            "actually happened), oldest first:"
        )
        for entry in declarations[-MAX_DECLARATIONS:]:
            lines.append(f"- turn {entry.get('turn')}: plan: {entry.get('plan')}")
            prediction = entry.get("prediction")
            if prediction:
                lines.append(f"  predicted: {prediction}")
            did = [row.get("action") for row in entry.get("did") or []]
            if did:
                lines.append(f"  did: {', '.join(did)}")
            opponent_did = [row.get("action") for row in entry.get("opponent_did") or []]
            if opponent_did:
                lines.append(f"  opponent did: {', '.join(opponent_did)}")
        lines.append("")

    advice = review_record.get("advice") or []
    if advice:
        lines.append(f"Advice {assignment.player} received or gave this game:")
        for entry in advice:
            approved = "approved" if entry.get("approved") else "not approved"
            followed = "followed" if entry.get("followed") else "not followed"
            lines.append(f"- {entry.get('summary')} ({approved}, {followed})")
        lines.append("")

    lines.append(
        "Write a short critique of how well " + assignment.player + " played: what the "
        "declared plans and their outcomes show about the seat's reads on the board, "
        "any turn where the actual play diverged from the stated plan without "
        "explanation, and one or two concrete things to play differently next time. "
        "Be specific and reference actual turns and cards where you can. This is an "
        "opinion for a human reader, not a rules ruling - do not invent board state "
        "you were not given."
    )
    return "\n".join(lines)
