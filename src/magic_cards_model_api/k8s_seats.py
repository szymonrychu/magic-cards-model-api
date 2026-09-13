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
) -> dict[str, str]:
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
    }


def render_seat_job_template(template: Any, values: Mapping[str, str]) -> Any:
    """Substitute `values` (a `SEAT_JOB_PLACEHOLDERS` -> string mapping) into
    a parsed Job template, recursively.

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


def _default_http(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=10.0)


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
        state = (
            "thinking"
            if driver.get("turnActive")
            else ("pending" if driver.get("pending") else "idle")
        )
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
            "reason": None,
            "pod": phase,
            "driver": driver,
            "activity": activity if isinstance(activity, dict) else None,
        }

    def release(self, uid: str) -> dict[str, Any]:
        with self._lock:
            seat = self._seats.pop(uid, None)
        if seat is None:
            return {"uid": uid, "mechanism": None, "deleted": False}
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
        result: dict[str, Any] = {
            "uid": uid,
            "mechanism": "kubernetes",
            "job": seat["job"],
            "deleted": deleted,
        }
        if reason is not None:
            result["reason"] = reason
        return result

    stop = release

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
