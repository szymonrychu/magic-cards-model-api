"""One Kubernetes Job per AI seat.

The API creates the Job, watches it, and reconciles; the Job carries the
agent container (the wrapper supervising one interactive claude) and the
driver as a sidecar, both from the same images the compose stack uses. The
driver takes its match from `DRIVER_ASSIGNMENT_JSON` and exits when the match
is over, so the Job completes on its own.

A Job rather than a bare Pod: the docs say a bare Pod is not rescheduled,
`activeDeadlineSeconds` bounds a runaway match, and `ttlSecondsAfterFinished`
cleans up. `ownerReferences` name this API's own Pod, so an API restart -
which loses every in-memory game anyway - garbage-collects its Jobs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .agent_settings import AgentSettings

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


CONTAINER_SECURITY = {
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
        metadata["ownerReferences"] = [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "name": settings.owner_pod_name,
                "uid": settings.owner_pod_uid,
            }
        ]
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
                "securityContext": CONTAINER_SECURITY,
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
                "securityContext": CONTAINER_SECURITY,
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
                    "failureThreshold": 36,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/readyz", "port": 8080},
                    "periodSeconds": 10,
                    "timeoutSeconds": 3,
                    "failureThreshold": 3,
                },
                "securityContext": CONTAINER_SECURITY,
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
                "securityContext": CONTAINER_SECURITY,
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
