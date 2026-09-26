"""One Kubernetes Job per AI seat.

The API creates the Job, watches it, and reconciles; the Job carries the
agent container (the wrapper supervising one interactive claude) and the
driver as a sidecar, both from the same images the compose stack uses. The
driver takes its match from `DRIVER_ASSIGNMENT_JSON` and exits when the match
is over, so the Job completes on its own.

A Job rather than a bare Pod: the docs say a bare Pod is not rescheduled,
`activeDeadlineSeconds` bounds a runaway match, and `ttlSecondsAfterFinished`
cleans up. There is deliberately no `ownerReferences` here (phase 2b T9/T10):
with durable games, an api restart must not garbage-collect the seat Jobs of
games that survived it, so `activeDeadlineSeconds`/`ttlSecondsAfterFinished`
are the whole backstop now, not a second line behind an owner-Pod GC.

`build_job` is a **renderer**, not a constructor: the Job body it returns is
read from a complete manifest at `settings.job_template_path` (env
`SEAT_JOB_TEMPLATE_PATH`, default `deploy/seat-job.yaml`) with the
`SEAT_JOB_PLACEHOLDERS` substituted (`render_seat_job_template`, and see that
file's own header comment for the full list and rationale) - `metadata.name`,
`metadata.namespace`, the identity labels and the annotations are set in code
afterward. This api no longer hardcodes the wrapper's 14 env names, the
driver's 10, or either one's console-script/module path: the template is the
one place that shape lives, owned by whoever deploys it.

F1 adds one more Job per unfinished handover, rendered from the same
template: `build_followup_job`'s follow-up DSL-development Job, one bounded
Opus turn run as the original seat's (or advisor's) uid, labelled
`LABEL_FOLLOWUP` instead of `LABEL_SEAT` and bounded by
`FOLLOWUP_ACTIVE_DEADLINE_SECONDS`. `K8sSeats.start_followup`,
`followup_running` and `release_followup` read and write it off the cluster
itself, so every replica answers the same; `reconcile` deletes it with its
game like any other Job.

The `memory` volume mounts the `AGENT_MEMORY_PVC` claim (default
`mtg-agent-memory`) into every concurrent seat's Pod at once, which requires
the claim's access mode to be `ReadWriteMany`. Today's
`deploy/k8s/30-storage.yaml` provisions it `ReadWriteOnce` - Task 10 must
switch it to `ReadWriteMany` before two seats can run concurrently, or the
second seat's Pod will stick in `ContainerCreating` waiting for the volume.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes import watch as k8s_watch
from kubernetes.client.exceptions import ApiException

from .agent_settings import AgentSettings

logger = logging.getLogger(__name__)

LABEL_APP = "app.kubernetes.io/name"
LABEL_GAME = "mtg/game-id"
LABEL_SEAT = "mtg/seat-uid"
#: Which kind of pod this is - set only on an advisor's Job (B5's buddy).
#: Absence means `seat`, deliberately: every Job in the cluster and every
#: reader of them predates this label, and stamping `mtg/role: seat` onto
#: them all would change every existing label set for no reader that wants
#: it. `LABEL_APP` stays `APP_LABEL_VALUE` on both kinds, because
#: `deploy/k8s/45-networkpolicy.yaml` selects the agent pods by it - a buddy
#: that fell out of that selector could reach neither the api nor the
#: Anthropic API.
LABEL_ROLE = "mtg/role"
APP_LABEL_VALUE = "mtg-agent"

#: What a runner is: a seat that plays, or an advisor (B5's buddy) that
#: reads one seat's own view and proposes. Defined here, of all places,
#: because this module needs them without being able to import `ai_seat` -
#: that dependency runs the other way - and re-exported from `ai_seat`,
#: which is where the rest of the api reads them from: the role is a
#: lifecycle fact, not a kubernetes one, and only its label value is ours.
#: The same two words are spelled out as a `Literal` on the two responses
#: that carry a role (`schemas.SeatLookupResponse`, `decks_api.SeatDetail`).
ROLE_SEAT = "seat"
ROLE_ADVISOR = "advisor"

#: Job-name prefix per role. A buddy is named `mtg-buddy-...` rather than
#: `mtg-seat-...` so `kubectl get jobs` says which kind of pod it is looking
#: at without reading a label off each one.
_JOB_PREFIX = {ROLE_SEAT: "mtg-seat", ROLE_ADVISOR: "mtg-buddy"}

#: F1's follow-up DSL-development Job carries this label instead of
#: `LABEL_SEAT`, valued with the seat (or advisor) uid it finishes the
#: handover of. Instead, not beside: `K8sSeats._pod` finds a seat's own pod
#: by `LABEL_SEAT`, and a follow-up pod answering that lookup would be read
#: as the seat itself. It is also the label the follow-up is deduped by.
LABEL_FOLLOWUP = "mtg/followup-of"

#: FD7 gap 15: a human seat's post-game review Job carries this label
#: instead of `LABEL_FOLLOWUP` - a human seat has no registered corpus and
#: no unfinished handover, so its post-game Job is never F1's own, and must
#: never be swept up, deduped or released as if it were one. See
#: `build_review_job`'s own docstring for exactly what this Job runs today,
#: and what is still missing to make it a real review.
LABEL_REVIEW = "mtg/review-of"

#: A seat's (or buddy's) Job deadline: the whole match plus its post-game
#: work. The runaway-match backstop `lobby.POSTGAME_RELEASE_SECONDS` sits
#: inside.
SEAT_ACTIVE_DEADLINE_SECONDS = 14400

#: A follow-up Job's deadline: one bounded Opus turn
#: (`mtg_agent.driver.prompt.FOLLOWUP_BUDGET_SECONDS`, 45 minutes) plus the
#: pod's and the agent's own boot. There is never a second follow-up, so this
#: is the whole of its life.
FOLLOWUP_ACTIVE_DEADLINE_SECONDS = 3600


@dataclass(frozen=True)
class K8sSettings:
    namespace: str = "mtg-engine"
    api_url: str = "http://mtg-stack.mtg-engine.svc.cluster.local:8000"
    #: The mcp service's own URL (M2: a stateless streamable-HTTP service,
    #: not an in-process stdio subprocess). Empty until a later stage's chart
    #: sets `MCP_SERVICE_URL` - the mcp service does not exist as its own
    #: deployment until the phase-2b cut. Empty is not a gap: the
    #: `mcp-config` initContainer (`deploy/seat-job.yaml`) falls back to a
    #: stdio `mtg` server run in-container from the agent image, the same
    #: shape `docker compose` already uses, so every seat gets a working
    #: `mtg` MCP server either way.
    mcp_url: str = ""
    #: Where `build_job` reads the seat Job template from - see that
    #: function and `deploy/seat-job.yaml`'s own header comment.
    job_template_path: str = "deploy/seat-job.yaml"
    #: How long a `Running` pod gets before an unreachable driver is treated
    #: as the driver container having died rather than "not up yet" -
    #: `restartPolicy: Never` means a dead driver container never comes back
    #: on its own, so without this a pod stuck exactly that way reports
    #: `pending` forever and `ai_pregame.py`'s backstop never fires.
    driver_grace_seconds: int = 60
    #: Set by `deploy/k8s/50-deployment.yaml`, rendered per-deploy with the
    #: pushed sha. Defaulted to the `:latest` tags only so a bare local run
    #: has something syntactically valid; a real cluster always sets both.
    agent_image: str = "harbor.szymonrichert.pl/containers/magic-cards-model-agent:latest"
    api_image: str = "harbor.szymonrichert.pl/containers/magic-cards-model-api:latest"
    #: Which revision of the deck-guides corpus
    #: (`github.com/szymonrychu/mtg-decks`) every seat Job checks out. The
    #: `guides` initContainer (`deploy/guides-clone.sh`) makes a writable
    #: clone the agent commits on, so it accepts only `main` and refuses
    #: anything else with a message saying so. Whatever it resolves to is
    #: recorded in the guides volume and ends up in the agent's briefing and
    #: in the game's plan file (`mtg_agent/driver/revision.py`).
    guides_ref: str = "main"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> K8sSettings:
        def value(key: str, default: str) -> str:
            return (env.get(key) or "").strip() or default

        return cls(
            namespace=value("AGENT_NAMESPACE", "mtg-engine"),
            api_url=value("API_SERVICE_URL", "http://mtg-stack.mtg-engine.svc.cluster.local:8000"),
            mcp_url=value("MCP_SERVICE_URL", ""),
            job_template_path=value("SEAT_JOB_TEMPLATE_PATH", "deploy/seat-job.yaml"),
            driver_grace_seconds=int(value("AGENT_DRIVER_GRACE_SECONDS", "60")),
            agent_image=value(
                "AGENT_IMAGE", "harbor.szymonrichert.pl/containers/magic-cards-model-agent:latest"
            ),
            api_image=value(
                "API_IMAGE", "harbor.szymonrichert.pl/containers/magic-cards-model-api:latest"
            ),
            guides_ref=value("GUIDES_REF", "main"),
        )


def followup_job_name(game_id: str, uid: str) -> str:
    """The follow-up Job's name for `uid`'s handover in `game_id`:
    `mtg-followup-<game>-<uid>`, one per (game, uid) by construction."""
    return f"mtg-followup-{game_id[:8]}-{uid[:8]}"


def review_job_name(game_id: str, uid: str) -> str:
    """The human-seat review Job's name for `uid` in `game_id`:
    `mtg-review-<game>-<uid>`, one per (game, uid) by construction - see
    `build_review_job`."""
    return f"mtg-review-{game_id[:8]}-{uid[:8]}"


def job_name(game_id: str, uid: str, role: str = ROLE_SEAT) -> str:
    """This Job's name: `mtg-seat-<game>-<uid>` for a seat,
    `mtg-buddy-<game>-<uid>` for an advisor. An unknown role is named as a
    seat rather than refused - a name is not the place to fail a dispatch,
    and `ROLE_ADVISOR` is the only value that changes anything."""
    return f"{_JOB_PREFIX.get(role, _JOB_PREFIX[ROLE_SEAT])}-{game_id[:8]}-{uid[:8]}"


#: The closed placeholder set a seat Job template may use (M10 of
#: docs/2b-plan.md's phase-2b plan) - every one of these, and nothing else,
#: gets substituted by `render_seat_job_template`. Each appears in the
#: template as the WHOLE value of some mapping entry, never embedded inside
#: a larger string, so substitution never has to reason about escaping.
SEAT_JOB_PLACEHOLDERS = (
    "__GAME_ID__",
    "__SEAT_UID__",
    "__PLAYER__",
    "__API_URL__",
    "__MCP_URL__",
    "__ASSIGNMENT_JSON__",
    "__MODEL__",
    "__EFFORT__",
    "__MAX_THINKING_TOKENS__",
    #: The two images. These are placeholders and not baked into the template
    #: on purpose: `mise run k8s:deploy` renders `50-deployment.yaml`'s
    #: `AGENT_IMAGE`/`API_IMAGE` with the sha it just pushed, and the seat Job
    #: has to name that same sha. A literal here is how every seat Job in the
    #: cluster ended up asking for a tag that never existed.
    "__AGENT_IMAGE__",
    "__API_IMAGE__",
    #: Which deck-guides revision the `guides` initContainer checks out
    #: (`K8sSettings.guides_ref`, default `main`).
    "__GUIDES_REF__",
    #: The agent container's own `MTG_ROLE` (`seat` or `advisor`) and
    #: `MTG_GAME_ID`. The driver already learns both from
    #: `__ASSIGNMENT_JSON__`; the agent reads no assignment, and needs them to
    #: name the branches and commits it makes in its writable guides checkout.
    "__MTG_ROLE__",
    "__MTG_GAME_ID__",
    #: `spec.activeDeadlineSeconds`: `SEAT_ACTIVE_DEADLINE_SECONDS` for a
    #: seat or a buddy, `FOLLOWUP_ACTIVE_DEADLINE_SECONDS` for F1's follow-up.
    #: The one placeholder whose value is an integer, not a string - the
    #: substitution swaps the whole leaf, so it lands as a YAML number.
    "__ACTIVE_DEADLINE_SECONDS__",
)


def _seat_job_values(
    *,
    game_id: str,
    uid: str,
    player: str,
    api_url: str,
    mcp_url: str,
    agent: AgentSettings,
    agent_image: str,
    api_image: str,
    guides_ref: str,
    role: str = ROLE_SEAT,
    followup: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    assignment: dict[str, Any] = {
        "uid": uid,
        "game_id": game_id,
        "player": player,
        "api_base": api_url,
        "agent": agent.to_wire(),
    }
    if role != ROLE_SEAT:
        # Written only for a non-default role, so a seat's payload stays
        # byte-identical to what every driver before B5 read - and
        # `DispatchIn.role` defaults to `seat` on the far end, so an absent
        # key means exactly what it always meant.
        assignment["role"] = role
    if followup is not None:
        # F1: the driver's `mode: "followup"` and everything it needs, built
        # server-side at schedule time. Absent on a seat's own payload, for
        # the same byte-identical reason as `role` above.
        assignment["mode"] = "followup"
        assignment["followup"] = dict(followup)
    elif mode is not None:
        # FD7 gap 15: the driver's `mode: "review"` - a review-only dispatch
        # that reads its deck from the lobby, not from a payload
        # (`driver/app.py`'s `assign_review`), so no `followup` key is ever
        # written for it.
        assignment["mode"] = mode
    return {
        "__GAME_ID__": game_id,
        "__SEAT_UID__": uid,
        "__PLAYER__": player,
        "__API_URL__": api_url,
        "__MCP_URL__": mcp_url,
        "__ASSIGNMENT_JSON__": json.dumps(assignment),
        "__MODEL__": agent.model,
        "__EFFORT__": agent.effort,
        "__MAX_THINKING_TOKENS__": (
            "" if agent.max_thinking_tokens is None else str(agent.max_thinking_tokens)
        ),
        "__AGENT_IMAGE__": agent_image,
        "__API_IMAGE__": api_image,
        "__GUIDES_REF__": guides_ref,
        "__MTG_ROLE__": role,
        "__MTG_GAME_ID__": game_id,
        "__ACTIVE_DEADLINE_SECONDS__": (
            SEAT_ACTIVE_DEADLINE_SECONDS
            if followup is None and mode is None
            else FOLLOWUP_ACTIVE_DEADLINE_SECONDS
        ),
    }


def render_seat_job_template(template: Any, values: Mapping[str, Any]) -> Any:
    """Substitute `values` (a `SEAT_JOB_PLACEHOLDERS` -> value mapping; every
    value a string but `__ACTIVE_DEADLINE_SECONDS__`'s integer) into a parsed
    Job template, recursively.

    Operates on already-parsed YAML (dicts/lists/scalars), not on raw text:
    every placeholder is the WHOLE value of some string leaf, so a leaf
    equal to one of `values`' keys is replaced outright, and nothing else is
    touched. This is what makes the substitution immune to YAML/JSON
    quoting - there is no text to escape, only a Python value to swap in.
    """
    if isinstance(template, dict):
        return {k: render_seat_job_template(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render_seat_job_template(v, values) for v in template]
    if isinstance(template, str) and template in values:
        return values[template]
    return template


def _load_seat_job_template(path: str) -> Any:
    return yaml.safe_load(Path(path).read_text())


def build_job(
    settings: K8sSettings,
    *,
    game_id: str,
    uid: str,
    player: str,
    agent: AgentSettings,
    role: str = ROLE_SEAT,
) -> dict[str, Any]:
    """Render one seat's Job body from `settings.job_template_path`.

    A renderer, not a constructor (phase 2b T9): the shape of the Job -
    every env name, the wrapper's console-script path, the driver's module
    path, the mcp-config initContainer - lives in the template file, not
    here. This function only supplies the per-seat values
    (`SEAT_JOB_PLACEHOLDERS`) the template can't know in advance, then sets
    the identity fields that are this api's own to set: `metadata.name`,
    `metadata.namespace` (kept in code, not baked into the template, so it
    can never drift from the namespace `K8sSeats` actually creates the Job
    in - a body/URL namespace mismatch is a hard Kubernetes API rejection,
    not a soft one), the identity labels, and the annotations.

    `role` is `ROLE_SEAT` or `ROLE_ADVISOR` (B5's buddy): it picks the name
    prefix, adds `LABEL_ROLE` for an advisor, rides into the driver's own
    self-dispatch payload through `__ASSIGNMENT_JSON__`, and reaches the
    agent container as `MTG_ROLE` through `__MTG_ROLE__`.
    """
    name = job_name(game_id, uid, role)
    labels = {LABEL_APP: APP_LABEL_VALUE, LABEL_GAME: game_id, LABEL_SEAT: uid}
    if role != ROLE_SEAT:
        labels[LABEL_ROLE] = role
    values = _seat_job_values(
        game_id=game_id,
        uid=uid,
        player=player,
        api_url=settings.api_url,
        mcp_url=settings.mcp_url,
        agent=agent,
        agent_image=settings.agent_image,
        api_image=settings.api_image,
        guides_ref=settings.guides_ref,
        role=role,
    )
    template = _load_seat_job_template(settings.job_template_path)
    job = render_seat_job_template(template, values)
    job["metadata"]["name"] = name
    job["metadata"]["namespace"] = settings.namespace
    job["metadata"]["labels"] = labels
    job["metadata"]["annotations"] = {
        "mtg/player": player,
        "mtg/model": agent.model,
        "mtg/effort": agent.effort,
    }
    job["spec"]["template"]["metadata"]["labels"] = labels
    return job


def build_followup_job(
    settings: K8sSettings,
    *,
    game_id: str,
    uid: str,
    player: str,
    role: str,
    agent: AgentSettings,
    followup: Mapping[str, Any],
) -> dict[str, Any]:
    """Render F1's follow-up DSL-development Job for `uid`'s unfinished
    handover in `game_id`, from the same template a seat renders from.

    The same pod - guides clone, agent, driver - because the follow-up is
    the same work the handover did: the agent commits through `mcp-seat` in
    a writable /guides clone, and the driver pushes one turn and files the
    `plays_outcome` row. What differs, all set here:

    - it runs as the ORIGINAL `uid` and `role`: the credential this game
      already bound to `player` (`game_access`), so it can act as that
      seat's player and nobody else, and no credential is minted or widened;
    - `agent` is the caller's (`AgentSettings.review_default()`, Opus);
    - the assignment carries `mode: "followup"` and `followup`, the payload
      built server-side from the ledger, so the driver pushes no turn 0;
    - its name is `followup_job_name`, and it is labelled `LABEL_FOLLOWUP`
      instead of `LABEL_SEAT` (see that label) - `LABEL_GAME` stays, so
      reconcile deletes it the moment its game is forgotten or ended;
    - `activeDeadlineSeconds` is `FOLLOWUP_ACTIVE_DEADLINE_SECONDS`.
    """
    labels = {LABEL_APP: APP_LABEL_VALUE, LABEL_GAME: game_id, LABEL_FOLLOWUP: uid}
    if role != ROLE_SEAT:
        labels[LABEL_ROLE] = role
    values = _seat_job_values(
        game_id=game_id,
        uid=uid,
        player=player,
        api_url=settings.api_url,
        mcp_url=settings.mcp_url,
        agent=agent,
        agent_image=settings.agent_image,
        api_image=settings.api_image,
        guides_ref=settings.guides_ref,
        role=role,
        followup=followup,
    )
    job = render_seat_job_template(_load_seat_job_template(settings.job_template_path), values)
    job["metadata"]["name"] = followup_job_name(game_id, uid)
    job["metadata"]["namespace"] = settings.namespace
    job["metadata"]["labels"] = labels
    job["metadata"]["annotations"] = {
        "mtg/player": player,
        "mtg/model": agent.model,
        "mtg/effort": agent.effort,
        "mtg/followup": "the DSL development an unfinished handover left open",
    }
    job["spec"]["template"]["metadata"]["labels"] = labels
    return job


def build_review_job(
    settings: K8sSettings, *, game_id: str, uid: str, player: str, agent: AgentSettings
) -> dict[str, Any]:
    """FD7 gap 15 (parity item 15), completed: a game with a human seat gets
    an automatic post-game review only in kubernetes mode, and only through
    this Job - no driver ever ran for the human's own perspective while the
    game was live, so there is no handover to leave unfinished and F1's own
    trigger (an `unfinished` `plays_outcome` row, `lobby.schedule_followup`)
    never fires for one.

    Dispatches `mode: "review"` - `driver/app.py`'s third `DispatchIn.mode`,
    `assign_review` - the fresh pod (the deck's guides clone, the same push
    credentials, `mtg-reviewer` identity) this Job already reached, running
    the real DR3 narrative review declaration (`post_ai_review`: the review
    record, the wrapper's `/v1/review`, `POST .../ai-review`) instead of
    `mode: "followup"`'s bounded DSL-development turn, which had nothing of
    a human seat's to redo. `assign_review` reads its own deck from the
    lobby, not from a payload, so no `followup` body is built here at all.

    Scheduling, deduping and releasing this Job is by its own label
    (`LABEL_REVIEW`, never `LABEL_FOLLOWUP` - a human seat's Job must not be
    mistaken for, or clash with, F1's own)."""
    labels = {LABEL_APP: APP_LABEL_VALUE, LABEL_GAME: game_id, LABEL_REVIEW: uid}
    values = _seat_job_values(
        game_id=game_id,
        uid=uid,
        player=player,
        api_url=settings.api_url,
        mcp_url=settings.mcp_url,
        agent=agent,
        agent_image=settings.agent_image,
        api_image=settings.api_image,
        guides_ref=settings.guides_ref,
        role=ROLE_SEAT,
        mode="review",
    )
    job = render_seat_job_template(_load_seat_job_template(settings.job_template_path), values)
    job["metadata"]["name"] = review_job_name(game_id, uid)
    job["metadata"]["namespace"] = settings.namespace
    job["metadata"]["labels"] = labels
    job["metadata"]["annotations"] = {
        "mtg/player": player,
        "mtg/model": agent.model,
        "mtg/effort": agent.effort,
        "mtg/review": "the human seat's own post-game review - it never had a driver of its own",
    }
    job["spec"]["template"]["metadata"]["labels"] = labels
    return job


def _job_finished(job: Any) -> bool:
    """A Job that has run to an end: a pod succeeded or failed (every Job
    here has `backoffLimit: 0`)."""
    status = getattr(job, "status", None)
    return bool(getattr(status, "succeeded", 0)) or bool(getattr(status, "failed", 0))


def _default_http(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=10.0)


#: How long a seat with a running game may have nothing playing it before it
#: reads as `stalled`. Longer than the driver's own recovery gaps - a turn
#: boundary before the continuation lands, its `UNREACHABLE_RETRY_SECONDS`
#: (5s) re-check of an unreachable game - so those never flash it.
STALLED_GRACE_SECONDS = 20.0


def stranded(driver: dict[str, Any]) -> bool:
    """A game still assigned and not yet reflected on, no turn in flight
    anywhere (`turnActive`, `agentBusy`), for longer than
    `STALLED_GRACE_SECONDS` (`idleSeconds`). A driver too old to report
    `idleSeconds` is judged without the grace."""
    if driver.get("turnActive") or driver.get("agentBusy"):
        return False
    if not driver.get("gameId") or driver.get("reflected"):
        return False
    idle = driver.get("idleSeconds")
    return idle is None or idle >= STALLED_GRACE_SECONDS


def driver_seat_state(driver: dict[str, Any]) -> str:
    """The seat state word for a reachable driver's `/status` with no error.

    `thinking` whenever claude holds a turn - one the driver pushed
    (`turnActive`) or one it did not (`agentBusy`, the wrapper's own session
    state: an agent woken by a background task, or a message sent to the
    wrapper directly). `turnActive` alone used to read such an agent as
    `idle` while it was mid-turn planning. `stalled` for a seat `stranded`
    says nothing has been playing past the grace, which is not a healthy
    idle; inside the grace it is still `thinking` (a turn boundary). `idle`
    is left for a driver with no game to play.
    """
    if driver.get("turnActive") or driver.get("agentBusy"):
        return "thinking"
    if driver.get("pending"):
        return "pending"
    if stranded(driver):
        return "stalled"
    if driver.get("gameId") and not driver.get("reflected"):
        # Inside the grace: between one turn and the next.
        return "thinking"
    return "idle"


class K8sSeats:
    """One Kubernetes Job per AI seat: create it, watch it, reconcile it
    against the games actually still live, and answer status/release for one
    uid at a time - the runner half of this module; `build_job` above only
    shapes the body it creates."""

    def __init__(
        self,
        settings: K8sSettings,
        batch_api: Any,
        core_api: Any,
        http_client_factory: Callable[[str], Any] = _default_http,
        reconcile_interval: float = 60.0,
    ) -> None:
        self.settings = settings
        self.batch = batch_api
        self.core = core_api
        self.http = http_client_factory
        #: How often `start_background`'s reconcile loop runs - also the
        #: grace period `reconcile()` gives a just-created seat before it is
        #: willing to call its Job "missing". `start()` inserts a seat into
        #: `_seats` before the (synchronous, but not lock-held) call to
        #: `create_namespaced_job` returns; a reconcile pass landing in that
        #: window would otherwise see the seat but not yet the Job and flag a
        #: perfectly healthy seat as an error forever (nothing else ever
        #: clears it). Tests that want the old "flag immediately" behaviour
        #: pass `reconcile_interval=0`.
        self.reconcile_interval = reconcile_interval
        # uid -> {game_id, player, job, error, created_at, missing,
        # last_pod_phase, gone}
        #
        # A per-process CACHE for `status()`/`start()` bookkeeping only -
        # never a source of truth for what is live (phase 2b T10).
        # `created_at` is a `time.monotonic()` reading, which is meaningless
        # across processes, and this dict only ever holds the uids *this*
        # process happened to `start()`, not every seat actually running.
        # `reconcile()`'s delete decision is deliberately independent of it:
        # it compares each Job's `mtg/game-id` label against the repository's
        # own live-game query, so every replica reconciles identically no
        # matter which one created which seat.
        self._seats: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(
        self,
        *,
        uid: str,
        game_id: str,
        player: str,
        agent: AgentSettings,
        role: str = ROLE_SEAT,
    ) -> dict[str, Any]:
        name = job_name(game_id, uid, role)
        with self._lock:
            already_started = uid in self._seats
            if not already_started:
                self._seats[uid] = {
                    "game_id": game_id,
                    "player": player,
                    "role": role,
                    "job": name,
                    "error": None,
                    "created_at": time.monotonic(),
                    "missing": False,
                    "last_pod_phase": None,
                    "gone": False,
                }
        # `status()` below takes `self._lock` itself - it must never be
        # called while this method still holds it (a plain `threading.Lock`
        # is not reentrant, so that reader-inside-writer shape deadlocks the
        # second `start()` call for an already-started uid).
        if already_started:
            return self.status(uid) or {}
        body = build_job(
            self.settings, game_id=game_id, uid=uid, player=player, agent=agent, role=role
        )
        try:
            self.batch.create_namespaced_job(self.settings.namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                self._forget_failed_create(uid, name, exc)
                raise
            # 409 AlreadyExists: idempotent - a Job by this name is already
            # out there (an earlier call's create that this process never
            # heard the answer to, say), so the seat stands and `status()`
            # below reports on the Job that already exists.
        except Exception as exc:
            self._forget_failed_create(uid, name, exc)
            raise
        return self.status(uid) or {}

    def _forget_failed_create(self, uid: str, name: str, exc: Exception) -> None:
        """Undo `start()`'s optimistic `_seats[uid]` insert when the create
        call it guarded actually failed.

        Left in place, that entry made the *next* `start()` for this uid see
        `already_started` and hand back `self.status(uid)` for a Job that was
        never created - reported as `pending` (no pod yet, nothing wrong)
        rather than the error it actually was, and audited as "scheduled"
        in `ai_seat.py`. Popping it here means the caller's retry inserts a
        fresh seat and genuinely tries to create the Job again; the exception
        is still re-raised so `ai_seat._dispatch_to_kubernetes` sees it and
        records the real reason instead of a seat that quietly never existed.
        """
        with self._lock:
            # `.pop(uid, None)`, not `del`: a `release()` racing this create
            # call may already have removed the seat by the time this runs,
            # and that must not raise either.
            self._seats.pop(uid, None)
        logger.error("job %s not created: %s", name, exc)

    def _pod(self, uid: str) -> Any | None:
        pods = self.core.list_namespaced_pod(
            self.settings.namespace, label_selector=f"{LABEL_SEAT}={uid}"
        ).items
        return pods[0] if pods else None

    def _driver_status(self, pod: Any) -> dict[str, Any] | None:
        ip = pod.status.pod_ip if pod is not None else None
        if not ip:
            return None
        try:
            with self.http(f"http://{ip}:8090") as client:
                response = client.get("/status")
                return response.json() if response.status_code < 400 else None
        except Exception:  # noqa: BLE001 - a driver that is not up yet
            return None

    def _driver_grace_elapsed(self, seat: dict[str, Any], pod: Any) -> float:
        """Seconds since this pod (or, absent a `start_time`, this seat) went
        live - the clock `status()` checks against `driver_grace_seconds`
        before calling an unreachable driver in a `Running` pod an error.

        `pod.status.start_time` is preferred: it is Kubernetes' own wall-clock
        answer for when this pod started running. `seat["created_at"]` (a
        `time.monotonic()` reading, taken when `start()` first inserted the
        seat) is the fallback for the brief window right after a pod turns
        `Running` before the apiserver has populated `start_time` at all.
        """
        start_time = getattr(pod.status, "start_time", None)
        if start_time is not None:
            now = (
                datetime.now(start_time.tzinfo) if start_time.tzinfo is not None else datetime.now()
            )
            return (now - start_time).total_seconds()
        return time.monotonic() - seat.get("created_at", time.monotonic())

    def status(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            seat = self._seats.get(uid)
        if seat is None:
            return None
        base = {
            "uid": uid,
            "mechanism": "kubernetes",
            "job": seat["job"],
            "game_id": seat["game_id"],
            "player": seat["player"],
            "role": seat.get("role", ROLE_SEAT),
        }
        if seat.get("gone"):
            # The Job finished cleanly (its pod's last known phase was
            # `Succeeded`/`Failed`) and has since been TTL-reaped or reconciled
            # away - the expected end of this seat's life, not a failure.
            return {
                **base,
                "state": "gone",
                "reason": seat.get("last_pod_phase") or "the Job was cleaned up",
                "pod": None,
                "driver": None,
            }
        if seat["error"]:
            return {**base, "state": "error", "reason": seat["error"], "pod": None, "driver": None}
        try:
            pod = self._pod(uid)
        except Exception as exc:  # noqa: BLE001 - a transient API hiccup is not "gone"
            return {
                **base,
                "state": "pending",
                "reason": f"could not read pod status: {exc}",
                "pod": None,
                "driver": None,
            }
        phase = pod.status.phase if pod is not None else None
        if pod is not None:
            # Remembered across calls so `reconcile()` can tell "the Job
            # vanished after finishing cleanly" (TTL cleanup - `gone`) from
            # "the Job vanished out from under a still-running seat" (`error`).
            with self._lock:
                seat["last_pod_phase"] = phase
        waiting = ""
        driver_terminated_reason = ""
        for cs in (pod.status.container_statuses or []) if pod is not None else []:
            state = getattr(cs, "state", None)
            if (
                state is not None
                and getattr(state, "waiting", None) is not None
                and state.waiting.reason
            ):
                waiting = state.waiting.reason
            terminated = getattr(state, "terminated", None) if state is not None else None
            if getattr(cs, "name", None) == "driver" and terminated is not None:
                # `restartPolicy: Never` means a dead driver container never
                # comes back on its own - caught here immediately, rather
                # than waiting out `driver_grace_seconds` below, because the
                # pod itself telling us the container is gone is stronger
                # evidence than an HTTP timeout.
                driver_terminated_reason = terminated.reason or "driver container terminated"
        if waiting in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"):
            return {**base, "state": "error", "reason": waiting, "pod": phase, "driver": None}
        if driver_terminated_reason:
            return {
                **base,
                "state": "error",
                "reason": driver_terminated_reason,
                "pod": phase,
                "driver": None,
            }
        # Terminal phases are decided before any driver call: the pod's own
        # IP can be recycled onto an unrelated pod the moment it is gone, so
        # asking "is the driver at this IP happy" is meaningless once the pod
        # itself has already succeeded or failed.
        if phase in ("Succeeded", "Failed"):
            return {**base, "state": "gone", "reason": phase, "pod": phase, "driver": None}
        if phase in (None, "Pending"):
            return {**base, "state": "pending", "reason": phase, "pod": phase, "driver": None}
        driver = self._driver_status(pod)
        if driver is None:
            if phase == "Running" and self._driver_grace_elapsed(seat, pod) >= (
                self.settings.driver_grace_seconds
            ):
                # `Running` and past the grace period with an unreachable
                # driver: in a Job with `restartPolicy: Never` that is not
                # "still booting" any more, it is the driver container having
                # died silently - the one case that otherwise reported
                # `pending` forever and never tripped the pregame backstop.
                return {
                    **base,
                    "state": "error",
                    "reason": "driver unreachable",
                    "pod": phase,
                    "driver": None,
                }
            return {**base, "state": "pending", "reason": phase, "pod": phase, "driver": None}
        if driver.get("lastError"):
            return {
                **base,
                "state": "error",
                "reason": driver["lastError"],
                "pod": phase,
                "driver": driver,
            }
        state = driver_seat_state(driver)
        # What the agent is doing right now, as one coarse word the driver
        # read off its own agent container's transcript watcher
        # (`mtg_agent/wrapper/activity.py`) - the play UI's AI pill shows it
        # instead of the static "thinking" it used to. Only on this branch:
        # every branch above is a seat that is not playing, and a failure or
        # a pod still booting must never be dressed up as an activity.
        activity = driver.get("agentActivity")
        return {
            **base,
            "state": state,
            "reason": (
                "the agent's turn ended while the game is still running"
                if state == "stalled"
                else None
            ),
            "pod": phase,
            "driver": driver,
            "activity": activity if isinstance(activity, dict) else None,
        }

    def _release_followup_quietly(self, uid: str) -> dict[str, Any]:
        try:
            return self.release_followup(uid)
        except Exception as exc:  # noqa: BLE001 - a release never 500s its caller
            logger.warning("could not release the follow-up of %s: %s", uid, exc)
            return {"deleted": [], "reason": str(exc)}

    def release(self, uid: str) -> dict[str, Any]:
        """Let go of `uid`: tell its driver, delete its Job - and its F1
        follow-up Job, if one runs, whether or not this process started the
        seat itself (the follow-up is found in the cluster by its label)."""
        followup = self._release_followup_quietly(uid)
        with self._lock:
            seat = self._seats.pop(uid, None)
        if seat is None:
            result: dict[str, Any] = {"uid": uid, "mechanism": None, "deleted": False}
            if followup["deleted"]:
                result.update(mechanism="kubernetes", followup=followup)
            return result
        try:
            pod = self._pod(uid)
        except Exception as exc:  # noqa: BLE001 - the Job is deleted regardless
            logger.info("could not read pod for %s before release: %s", uid, exc)
            pod = None
        if pod is not None and pod.status.pod_ip:
            try:
                with self.http(f"http://{pod.status.pod_ip}:8090") as client:
                    client.post("/release", json={"uid": uid, "game_id": seat["game_id"]})
            except Exception as exc:  # noqa: BLE001 - the Job is deleted regardless
                logger.info("driver for %s not told about the release: %s", uid, exc)
        deleted = True
        reason: str | None = None
        try:
            self.batch.delete_namespaced_job(
                seat["job"],
                self.settings.namespace,
                body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as exc:
            deleted = exc.status == 404
            if not deleted:
                reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - a transport error must not 500 the caller
            # Narrower than `ApiException` used to catch only the Kubernetes
            # client's own typed errors - a transport failure (a DNS hiccup,
            # a dropped connection) is neither of those, propagated straight
            # up through `release()` into `DELETE /api/ui/games/{id}` as an
            # unhandled 500, leaving the game undeleted with no reason given.
            deleted = False
            reason = str(exc)
            logger.warning("could not delete job %s for %s: %s", seat["job"], uid, exc)
        result = {
            "uid": uid,
            "mechanism": "kubernetes",
            "job": seat["job"],
            "deleted": deleted,
            "followup": followup,
        }
        if reason is not None:
            result["reason"] = reason
        return result

    stop = release

    # -- F1: the follow-up DSL-development Job -------------------------------
    def _followup_jobs(self, uid: str) -> list[Any]:
        """Every follow-up Job of `uid` in the cluster, read from the
        cluster itself - never from this process's cache - so every replica
        answers the same. Filtered on the labels here as well as in the
        selector, so a lister that ignores the selector cannot widen it."""
        jobs = self.batch.list_namespaced_job(
            self.settings.namespace, label_selector=f"{LABEL_FOLLOWUP}={uid}"
        ).items
        return [job for job in jobs if (job.metadata.labels or {}).get(LABEL_FOLLOWUP) == uid]

    def start_followup(
        self,
        *,
        uid: str,
        game_id: str,
        player: str,
        role: str,
        agent: AgentSettings,
        followup: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create `uid`'s one follow-up Job (`build_followup_job`), unless the
        cluster already holds one for it: deduped by `LABEL_FOLLOWUP`, and
        a 409 AlreadyExists on the deterministic name reads the same. The
        answer's `state` is `scheduled` or `exists`. A create failure
        raises, for the registry to report."""
        name = followup_job_name(game_id, uid)
        base = {
            "uid": uid,
            "mechanism": "kubernetes",
            "job": name,
            "game_id": game_id,
            "player": player,
            "role": role,
        }
        if any(
            (job.metadata.labels or {}).get(LABEL_GAME) == game_id
            for job in self._followup_jobs(uid)
        ):
            return {**base, "state": "exists"}
        body = build_followup_job(
            self.settings,
            game_id=game_id,
            uid=uid,
            player=player,
            role=role,
            agent=agent,
            followup=followup,
        )
        try:
            self.batch.create_namespaced_job(self.settings.namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            return {**base, "state": "exists"}
        logger.info("follow-up job %s created for %s (%s)", name, player, role)
        return {**base, "state": "scheduled"}

    def followup_running(self, uid: str) -> bool:
        """Whether a follow-up Job of `uid` is still in the cluster and has
        not run to an end. Raises when the cluster cannot be read - the
        caller decides what an unknown answer means."""
        return any(not _job_finished(job) for job in self._followup_jobs(uid))

    def release_followup(self, uid: str) -> dict[str, Any]:
        """Delete every follow-up Job of `uid`: End game, and the post-game
        backstop, stop it here; Forget reaches it through `reconcile`. Its
        pod's preStop hands the driver a `/handover`, which in follow-up
        mode stops the turn and writes the marker at once."""
        deleted: list[str] = []
        for job in self._followup_jobs(uid):
            try:
                self.batch.delete_namespaced_job(
                    job.metadata.name,
                    self.settings.namespace,
                    body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
                )
            except ApiException as exc:
                if exc.status != 404:
                    logger.warning("could not delete follow-up job %s: %s", job.metadata.name, exc)
                    continue
            deleted.append(job.metadata.name)
        return {"deleted": deleted}

    # -- FD7 gap 15: the human-seat review Job -------------------------------
    def _review_jobs(self, uid: str) -> list[Any]:
        """Every review Job of `uid` in the cluster - the same read-from-the-
        cluster, filter-on-the-label shape `_followup_jobs` uses, over
        `LABEL_REVIEW` instead."""
        jobs = self.batch.list_namespaced_job(
            self.settings.namespace, label_selector=f"{LABEL_REVIEW}={uid}"
        ).items
        return [job for job in jobs if (job.metadata.labels or {}).get(LABEL_REVIEW) == uid]

    def start_review_followup(
        self, *, uid: str, game_id: str, player: str, agent: AgentSettings
    ) -> dict[str, Any]:
        """Create `uid`'s one human-seat review Job (`build_review_job`),
        unless the cluster already holds one for this game: deduped by
        `LABEL_REVIEW`, and a 409 AlreadyExists on the deterministic name
        reads the same. The answer's `state` is `scheduled` or `exists`. A
        create failure raises, for the registry to report."""
        name = review_job_name(game_id, uid)
        base = {
            "uid": uid,
            "mechanism": "kubernetes",
            "job": name,
            "game_id": game_id,
            "player": player,
        }
        if any(
            (job.metadata.labels or {}).get(LABEL_GAME) == game_id for job in self._review_jobs(uid)
        ):
            return {**base, "state": "exists"}
        body = build_review_job(self.settings, game_id=game_id, uid=uid, player=player, agent=agent)
        try:
            self.batch.create_namespaced_job(self.settings.namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            return {**base, "state": "exists"}
        logger.info("review job %s created for %s", name, player)
        return {**base, "state": "scheduled"}

    def review_followup_running(self, uid: str) -> bool:
        """Whether a human-seat review Job of `uid` is still in the cluster
        and has not run to an end. Raises when the cluster cannot be read -
        the caller decides what an unknown answer means."""
        return any(not _job_finished(job) for job in self._review_jobs(uid))

    def release_review_followup(self, uid: str) -> dict[str, Any]:
        """Delete every human-seat review Job of `uid` - the same shape
        `release_followup` deletes F1's own by, over `_review_jobs`."""
        deleted: list[str] = []
        for job in self._review_jobs(uid):
            try:
                self.batch.delete_namespaced_job(
                    job.metadata.name,
                    self.settings.namespace,
                    body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
                )
            except ApiException as exc:
                if exc.status != 404:
                    logger.warning("could not delete review job %s: %s", job.metadata.name, exc)
                    continue
            deleted.append(job.metadata.name)
        return {"deleted": deleted}

    # -- tracking ------------------------------------------------------------
    def reconcile(self, live_game_ids: Callable[[], set[str]]) -> dict[str, list[str]]:
        """Delete every seat Job whose game is no longer live, and flag any
        seat this process is tracking whose Job has vanished.

        `live_game_ids` is called here, not handed a pre-computed set: this
        is what turns "refuse to delete anything if the live-set query
        failed" into a property of `reconcile` itself, provable with a fake
        query, rather than something that only holds because every current
        caller happens to wrap the query in its own try/except. An empty
        result and a failed query are not the same thing - a query that
        raises is a database that could not answer, not a database that
        answered "nothing is live"; treating the two the same deletes every
        seat Job in the namespace in one pass. So a raise here is caught,
        nothing is deleted, and this pass reports an empty `{"deleted": [],
        "missing": []}` rather than acting on a set it never actually got.
        """
        try:
            live_ids = live_game_ids()
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown", not "empty"
            logger.warning(
                "reconcile: live-game id query failed; refusing to delete any Job: %s", exc
            )
            return {"deleted": [], "missing": []}
        jobs = self.batch.list_namespaced_job(
            self.settings.namespace, label_selector=f"{LABEL_APP}={APP_LABEL_VALUE}"
        ).items
        present = {job.metadata.name for job in jobs}
        deleted: list[str] = []
        for job in jobs:
            game_id = (job.metadata.labels or {}).get(LABEL_GAME)
            if game_id not in live_ids:
                try:
                    self.batch.delete_namespaced_job(
                        job.metadata.name,
                        self.settings.namespace,
                        body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
                    )
                    deleted.append(job.metadata.name)
                except ApiException as exc:
                    logger.warning("could not delete stray job %s: %s", job.metadata.name, exc)
        missing: list[str] = []
        now = time.monotonic()
        with self._lock:
            for uid, seat in self._seats.items():
                job_present = seat["job"] in present
                if job_present:
                    # The Job reappeared (or never really left - a prior pass
                    # flagged it while `start()` was still mid-create): this
                    # was never actually missing, so undo that verdict rather
                    # than leaving a healthy seat pinned at `error` forever.
                    if seat.get("missing"):
                        seat["error"] = None
                        seat["missing"] = False
                    continue
                if seat.get("gone") or seat["error"]:
                    continue  # already accounted for, one way or another
                age = now - seat.get("created_at", now)
                if age < self.reconcile_interval:
                    # Too young to judge: `create_namespaced_job` may simply
                    # not have returned yet when this pass listed Jobs.
                    continue
                if seat.get("last_pod_phase") in ("Succeeded", "Failed"):
                    # The Job finished cleanly and has since been TTL-reaped
                    # (or reconciled away as a dead game's stray Job above) -
                    # the expected end of this seat's life, not a failure.
                    seat["gone"] = True
                    continue
                seat["error"] = f"job {seat['job']} is missing from the cluster"
                seat["missing"] = True
                missing.append(uid)
        return {"deleted": deleted, "missing": missing}

    def run_watch(self, stop: threading.Event, sleep: Callable[[float], None] = time.sleep) -> None:
        """Watch Jobs until `stop` is set, relisting whenever the stream ends.

        A short, unconditional `sleep` follows every outer iteration - not
        only the exception paths. A stream that ends *without* raising (the
        server closing it cleanly, or a fake in a test whose `.stream(...)`
        simply returns) would otherwise spin this loop with no delay at all;
        `sleep` is a parameter rather than a bare `time.sleep(1)` so a test can
        inject a recording stand-in instead of a real one-second wait.
        """
        while not stop.is_set():
            try:
                w = k8s_watch.Watch()
                for event in w.stream(
                    self.batch.list_namespaced_job,
                    self.settings.namespace,
                    label_selector=f"{LABEL_APP}={APP_LABEL_VALUE}",
                    timeout_seconds=300,
                ):
                    job = event["object"]
                    logger.info(
                        "job %s %s active=%s succeeded=%s failed=%s",
                        job.metadata.name,
                        event["type"],
                        job.status.active,
                        job.status.succeeded,
                        job.status.failed,
                    )
                    if stop.is_set():
                        break
            except ApiException as exc:
                logger.warning("job watch ended: %s; relisting", exc.reason)
            except Exception as exc:  # noqa: BLE001 - keep watching
                logger.warning("job watch error: %s", exc)
            if not stop.is_set():
                sleep(1.0)

    def start_background(
        self, live_games: Callable[[], set[str]], interval: float | None = None
    ) -> threading.Event:
        interval = self.reconcile_interval if interval is None else interval
        stop = threading.Event()
        threading.Thread(
            target=self.run_watch, args=(stop,), name="k8s-seat-watch", daemon=True
        ).start()

        def loop() -> None:
            while not stop.wait(interval):
                try:
                    report = self.reconcile(live_games)
                    if report["deleted"] or report["missing"]:
                        logger.info("reconcile: %s", report)
                except Exception as exc:  # noqa: BLE001 - reconcile again next tick
                    logger.warning("reconcile failed: %s", exc)

        threading.Thread(target=loop, name="k8s-seat-reconcile", daemon=True).start()
        return stop


_seats: K8sSeats | None = None
#: Guards the whole build-and-start body below, not just the assignment to
#: `_seats`. Without it, two threadpool routes racing their first call to
#: `get()` both see `_seats is None`, both build a `K8sSeats`, and both start
#: a watch/reconcile thread pair - the first object (and its watch/reconcile
#: threads) is then simply discarded, orphaned but still running, while the
#: seats it was tracking are invisible to the `K8sSeats` that wins the race.
_lock = threading.Lock()


def get() -> K8sSeats:
    """The process-wide `K8sSeats`, created lazily on first use so importing
    this module never requires a cluster or kubeconfig - only `AI_SEAT_MODE=
    kubernetes` calling this reaches for one. `set_for_tests` overrides it
    (and `None` clears the override), which is how tests exercise
    `ai_seat.py`'s kubernetes branch against the same fakes `K8sSeats` itself
    is tested with, without ever touching a real cluster.

    Double-checked locking: the cheap, lock-free read lets every call after
    the first skip the lock entirely; `_lock` only serialises the handful of
    threads that can race the very first call.

    The background watch/reconcile threads are started *before* the initial
    `reconcile()` call, and that call is wrapped in a broad `except Exception`
    - a reconcile that fails (an apiserver hiccup on the very first call, say)
    must not leave `_seats` set with no background threads behind it: that
    would be a `K8sSeats` that looks alive but whose Jobs are never watched or
    reconciled again, silently, for the rest of the process's life.
    """
    global _seats
    if _seats is not None:
        return _seats
    with _lock:
        if _seats is not None:
            return _seats
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        settings = K8sSettings.from_env(os.environ)
        seats = K8sSeats(settings, k8s_client.BatchV1Api(), k8s_client.CoreV1Api())
        from .store import store  # lazy: store imports nothing from here

        live_game_ids = lambda: set(store.ids())  # noqa: E731 - shared by both calls below
        seats.start_background(live_game_ids)
        try:
            seats.reconcile(live_game_ids)
        except Exception:  # noqa: BLE001 - background reconcile will retry
            logger.warning(
                "initial reconcile failed; background reconcile will retry", exc_info=True
            )
        _seats = seats
        return _seats


def set_for_tests(seats: K8sSeats | None) -> None:
    """Test-only override for `get()` - see its docstring."""
    global _seats
    _seats = seats
