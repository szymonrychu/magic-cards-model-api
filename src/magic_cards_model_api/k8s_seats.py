"""One Kubernetes Job per AI seat.

The API creates the Job, watches it, and reconciles; the Job carries the
agent container (the wrapper supervising one interactive claude) and the
driver as a sidecar, both from the same images the compose stack uses. The
driver takes its match from `DRIVER_ASSIGNMENT_JSON` and exits when the match
is over, so the Job completes on its own.

A Job rather than a bare Pod: the docs say a bare Pod is not rescheduled,
`activeDeadlineSeconds` bounds a runaway match, and `ttlSecondsAfterFinished`
cleans up. `ownerReferences` name this API's own Pod, so an API restart -
which loses every in-memory game anyway - garbage-collects its Jobs. That
owner reference is only safe when the owner Pod lives in the same namespace
as the Job: a cross-namespace `ownerReferences` entry makes the garbage
collector delete the Job within seconds of creation. `build_job` therefore
emits it only when `owner_pod_namespace` (from the downward API's
`POD_NAMESPACE`, i.e. `metadata.namespace`; Task 10 sets it) equals the Job's
own `namespace`, and logs a warning instead when they differ.

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
from typing import Any

import httpx
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes import watch as k8s_watch
from kubernetes.client.exceptions import ApiException

from .agent_settings import AgentSettings

logger = logging.getLogger(__name__)

LABEL_APP = "app.kubernetes.io/name"
LABEL_GAME = "mtg/game-id"
LABEL_SEAT = "mtg/seat-uid"
APP_LABEL_VALUE = "mtg-agent"


@dataclass(frozen=True)
class K8sSettings:
    namespace: str = "mtg-engine"
    agent_image: str = ""
    api_image: str = ""
    api_url: str = "http://mtg-stack.mtg-engine.svc.cluster.local:8000"
    spellslinger_url: str = ""
    owner_pod_name: str = ""
    owner_pod_uid: str = ""
    owner_pod_namespace: str = ""
    deadline_seconds: int = 14400
    ttl_seconds: int = 600
    memory_claim: str = "mtg-agent-memory"
    guides_repo: str = "https://github.com/szymonrychu/mtg-decks.git"
    agent_secret: str = "mtg-agent"
    git_secret: str = "mtg-decks-git"
    image_pull_secret: str = "regcred"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> K8sSettings:
        def value(key: str, default: str) -> str:
            return (env.get(key) or "").strip() or default

        return cls(
            namespace=value("AGENT_NAMESPACE", "mtg-engine"),
            agent_image=value("AGENT_IMAGE", ""),
            api_image=value("API_IMAGE", ""),
            api_url=value("API_SERVICE_URL", "http://mtg-stack.mtg-engine.svc.cluster.local:8000"),
            spellslinger_url=value("SPELLSLINGER_MCP_URL", ""),
            owner_pod_name=value("POD_NAME", ""),
            owner_pod_uid=value("POD_UID", ""),
            owner_pod_namespace=value("POD_NAMESPACE", ""),
            deadline_seconds=int(value("AGENT_JOB_DEADLINE_SECONDS", "14400")),
            ttl_seconds=int(value("AGENT_JOB_TTL_SECONDS", "600")),
            memory_claim=value("AGENT_MEMORY_PVC", "mtg-agent-memory"),
            guides_repo=value("GUIDES_REPO", "https://github.com/szymonrychu/mtg-decks.git"),
            agent_secret=value("AGENT_SECRET", "mtg-agent"),
            git_secret=value("GUIDES_GIT_SECRET", "mtg-decks-git"),
            image_pull_secret=value("IMAGE_PULL_SECRET", "regcred"),
        )


def job_name(game_id: str, uid: str) -> str:
    return f"mtg-seat-{game_id[:8]}-{uid[:8]}"


def mcp_config(settings: K8sSettings, game_id: str) -> dict[str, Any]:
    servers: dict[str, Any] = {
        "mtg": {
            "command": "python",
            "args": [
                "-m",
                "magic_cards_model_api.mcp",
                "--game",
                game_id,
                "--api",
                settings.api_url,
            ],
        }
    }
    if settings.spellslinger_url:
        servers["spellslinger"] = {"type": "http", "url": settings.spellslinger_url}
    return {"mcpServers": servers}


def _env(pairs: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in pairs.items()]


def container_security() -> dict[str, Any]:
    """A fresh per-container securityContext dict.

    Not a shared module-level constant: the Kubernetes client's
    `ApiClient.sanitize_for_serialization` branches on `isinstance(obj, dict)`
    and falls into its model-attribute branch for anything else (a
    `types.MappingProxyType`, say), raising `AttributeError` rather than
    serializing it. Every `securityContext` site gets its own plain dict.
    """
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
    }


def build_job(
    settings: K8sSettings, *, game_id: str, uid: str, player: str, agent: AgentSettings
) -> dict[str, Any]:
    name = job_name(game_id, uid)
    labels = {LABEL_APP: APP_LABEL_VALUE, LABEL_GAME: game_id, LABEL_SEAT: uid}
    assignment = {
        "uid": uid,
        "game_id": game_id,
        "player": player,
        "api_base": settings.api_url,
        "agent": agent.model_dump(),
    }
    agent_env = {
        "HTTP_ADDR": ":8080",
        "INTERNAL_ADDR": "127.0.0.1:8099",
        "WORKSPACE": "/workspace",
        "HOME_DIR": "/agent-home",
        "HOOK_COMMAND": "/app/.venv/bin/cc-stop-hook",
        "CCW_INTERNAL_URL": "http://127.0.0.1:8099/internal/turn-complete",
        "CCW_RESULT_JSON": "/workspace/result.json",
        "BASE_MCP_PATH": "/etc/mtg/mcp.json",
        "TURN_TIMEOUT_SECONDS": "1800",
        "BOOT_TIMEOUT_SECONDS": "120",
        "LOG_LEVEL": "info",
        "AGENT_LOG_LEVEL": "tools",
        "ACCESS_LOG_QUIET": "1",
        **agent.env(),
    }
    driver_env = {
        "GAME_API": settings.api_url,
        "WRAPPER_API": "http://127.0.0.1:8080",
        "DRIVER_SELF_URL": "http://127.0.0.1:8090",
        "GUIDES_ROOT": "/guides",
        "MEMORY_ROOT": "/workspace/memory",
        "DRIVER_WORKSPACE": "/driver-workspace",
        "HTTP_ADDR": ":8090",
        "ACCESS_LOG_QUIET": "1",
        "DRIVER_EXIT_AFTER_MATCH": "1",
        "DRIVER_ASSIGNMENT_JSON": json.dumps(assignment),
    }
    metadata: dict[str, Any] = {
        "name": name,
        "namespace": settings.namespace,
        "labels": labels,
        "annotations": {"mtg/player": player, "mtg/model": agent.model, "mtg/effort": agent.effort},
    }
    if settings.owner_pod_name and settings.owner_pod_uid:
        if settings.owner_pod_namespace == settings.namespace:
            metadata["ownerReferences"] = [
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "name": settings.owner_pod_name,
                    "uid": settings.owner_pod_uid,
                }
            ]
        else:
            logger.warning(
                "owner pod %s is in namespace %r but Job %s is in namespace %r; "
                "omitting ownerReferences to avoid cross-namespace garbage collection",
                settings.owner_pod_name,
                settings.owner_pod_namespace,
                name,
                settings.namespace,
            )
    guides_host_and_path = settings.guides_repo.removeprefix("https://")
    guides_script = (
        'test -n "${GH_PAT}" || { echo "GH_PAT is empty" >&2; exit 1; }\n'
        "git config --global --add safe.directory /guides\n"
        "find /guides -mindepth 1 -delete\n"
        f'git clone --depth 1 "https://x-access-token:${{GH_PAT}}@{guides_host_and_path}" /guides\n'
        f"git -C /guides remote set-url origin {settings.guides_repo}\n"
        '! grep -rq "x-access-token" /guides/.git/config\n'
    )
    mcp_json = json.dumps(mcp_config(settings, game_id))
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "imagePullSecrets": [{"name": settings.image_pull_secret}],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumes": [
            {"name": "guides", "emptyDir": {}},
            {"name": "memory", "persistentVolumeClaim": {"claimName": settings.memory_claim}},
            {"name": "driver-workspace", "emptyDir": {}},
            {"name": "mcp", "emptyDir": {}},
        ],
        "initContainers": [
            {
                "name": "guides",
                "image": settings.agent_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-euc", guides_script],
                "env": [
                    {
                        "name": "GH_PAT",
                        "valueFrom": {
                            "secretKeyRef": {"name": settings.git_secret, "key": "GH_PAT"}
                        },
                    },
                    {"name": "GIT_TERMINAL_PROMPT", "value": "0"},
                    {"name": "HOME", "value": "/agent-home"},
                ],
                "volumeMounts": [{"name": "guides", "mountPath": "/guides"}],
                "securityContext": container_security(),
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
            },
            {
                "name": "mcp-config",
                "image": settings.agent_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-euc", "printf '%s' \"$MCP_JSON\" > /etc/mtg/mcp.json"],
                "env": [{"name": "MCP_JSON", "value": mcp_json}],
                "volumeMounts": [{"name": "mcp", "mountPath": "/etc/mtg"}],
                "securityContext": container_security(),
                "resources": {
                    "requests": {"cpu": "10m", "memory": "16Mi"},
                    "limits": {"cpu": "100m", "memory": "64Mi"},
                },
            },
        ],
        "containers": [
            {
                "name": "agent",
                "image": settings.agent_image,
                "imagePullPolicy": "IfNotPresent",
                "ports": [{"name": "agent", "containerPort": 8080}],
                "env": _env(agent_env),
                "envFrom": [{"secretRef": {"name": settings.agent_secret}}],
                "volumeMounts": [
                    {"name": "guides", "mountPath": "/guides", "readOnly": True},
                    {"name": "memory", "mountPath": "/workspace/memory"},
                    {"name": "mcp", "mountPath": "/etc/mtg", "readOnly": True},
                ],
                "startupProbe": {
                    "httpGet": {"path": "/healthz", "port": 8080},
                    "periodSeconds": 10,
                    "timeoutSeconds": 3,
                    "failureThreshold": 36,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/readyz", "port": 8080},
                    "periodSeconds": 10,
                    "timeoutSeconds": 5,
                    "failureThreshold": 3,
                },
                "securityContext": container_security(),
                "resources": {
                    "requests": {"cpu": "250m", "memory": "1Gi"},
                    "limits": {"cpu": "2", "memory": "3Gi"},
                },
            },
            {
                "name": "driver",
                "image": settings.api_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-m", "mtg_agent.driver"],
                "ports": [{"name": "driver", "containerPort": 8090}],
                "env": _env(driver_env),
                "volumeMounts": [
                    {"name": "guides", "mountPath": "/guides", "readOnly": True},
                    {"name": "memory", "mountPath": "/workspace/memory"},
                    {"name": "driver-workspace", "mountPath": "/driver-workspace"},
                ],
                "readinessProbe": {
                    "httpGet": {"path": "/healthz", "port": 8090},
                    "periodSeconds": 10,
                    "timeoutSeconds": 3,
                    "failureThreshold": 3,
                },
                "securityContext": container_security(),
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            },
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": settings.deadline_seconds,
            "ttlSecondsAfterFinished": settings.ttl_seconds,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


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
        self._seats: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, uid: str, game_id: str, player: str, agent: AgentSettings) -> dict[str, Any]:
        name = job_name(game_id, uid)
        with self._lock:
            already_started = uid in self._seats
            if not already_started:
                self._seats[uid] = {
                    "game_id": game_id,
                    "player": player,
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
        body = build_job(self.settings, game_id=game_id, uid=uid, player=player, agent=agent)
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
        for cs in (pod.status.container_statuses or []) if pod is not None else []:
            state = getattr(cs, "state", None)
            if (
                state is not None
                and getattr(state, "waiting", None) is not None
                and state.waiting.reason
            ):
                waiting = state.waiting.reason
        if waiting in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"):
            return {**base, "state": "error", "reason": waiting, "pod": phase, "driver": None}
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
        return {**base, "state": state, "reason": None, "pod": phase, "driver": driver}

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
        try:
            self.batch.delete_namespaced_job(
                seat["job"],
                self.settings.namespace,
                body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as exc:
            deleted = exc.status == 404
        return {"uid": uid, "mechanism": "kubernetes", "job": seat["job"], "deleted": deleted}

    stop = release

    # -- tracking ------------------------------------------------------------
    def reconcile(self, live_game_ids: set[str]) -> dict[str, list[str]]:
        jobs = self.batch.list_namespaced_job(
            self.settings.namespace, label_selector=f"{LABEL_APP}={APP_LABEL_VALUE}"
        ).items
        present = {job.metadata.name for job in jobs}
        deleted: list[str] = []
        for job in jobs:
            game_id = (job.metadata.labels or {}).get(LABEL_GAME)
            if game_id not in live_game_ids:
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
                    report = self.reconcile(live_games())
                    if report["deleted"] or report["missing"]:
                        logger.info("reconcile: %s", report)
                except Exception as exc:  # noqa: BLE001 - reconcile again next tick
                    logger.warning("reconcile failed: %s", exc)

        threading.Thread(target=loop, name="k8s-seat-reconcile", daemon=True).start()
        return stop


_seats: K8sSeats | None = None


def get() -> K8sSeats:
    """The process-wide `K8sSeats`, created lazily on first use so importing
    this module never requires a cluster or kubeconfig - only `AI_SEAT_MODE=
    kubernetes` calling this reaches for one. `set_for_tests` overrides it
    (and `None` clears the override), which is how tests exercise
    `ai_seat.py`'s kubernetes branch against the same fakes `K8sSeats` itself
    is tested with, without ever touching a real cluster."""
    global _seats
    if _seats is None:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        settings = K8sSettings.from_env(os.environ)
        _seats = K8sSeats(settings, k8s_client.BatchV1Api(), k8s_client.CoreV1Api())
        from .store import store  # lazy: store imports nothing from here

        _seats.reconcile(set(store.ids()))
        _seats.start_background(lambda: set(store.ids()))
    return _seats


def set_for_tests(seats: K8sSeats | None) -> None:
    """Test-only override for `get()` - see its docstring."""
    global _seats
    _seats = seats
