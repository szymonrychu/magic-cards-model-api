"""Post-deploy smoke check, run INSIDE the mtg-stack `game` container.

`mise run k8s:smoke <version>` pipes this file to `python -` through
`kubectl exec -i`, so it runs on the image's own Python with the image's own
environment and code, against the pod's own listener on 127.0.0.1:8000. It
needs no dependency beyond the standard library and the application package.

Checks, one `PASS`/`FAIL` line each, exit status 1 if any failed:

- `GET /api/health` is 200 with `status: ok`;
- the sim MCP server (`/sim/mcp`, bearer `MTG_SIM_TOKEN` from the environment)
  lists the chat-sandbox tools this release ships and none of the retired ones;
- `AgentSettings.default(os.environ)` is the lobby default (claude-opus-5-5, low);
- a seat Job rendered by `k8s_seats.build_job` does not set `CLAUDE_CODE_EFFORT_LEVEL`.

The bearer token is only ever sent in the Authorization header. It is never
printed, and any error text is scrubbed of it before it is.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

BASE_URL = "http://127.0.0.1:8000"
MCP_PATH = "/sim/mcp"
HEALTH_PATH = "/api/health"
PROTOCOL_VERSION = "2025-03-26"

#: Tools that must NOT be offered any more (retired with the Claude-match rework).
ABSENT_TOOLS = ("sim_match", "sim_match_status", "sim_play_new")
#: Tools this release adds; a rollout that is still on the old image lacks them.
PRESENT_TOOLS = ("sim_review_mtgo_log", "sim_card_lookup", "sim_snapshot", "sim_restore")

EXPECTED_MODEL = "claude-opus-5-5"
EXPECTED_EFFORT = "low"
FORBIDDEN_SEAT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"


class Result(NamedTuple):
    name: str
    ok: bool
    detail: str = ""


def check_health(status: int, body: Mapping[str, Any]) -> Result:
    ok = status == 200 and body.get("status") == "ok"
    return Result("health", ok, f"HTTP {status}, status={body.get('status')!r}")


def check_tools(names: Iterable[str]) -> list[Result]:
    listed = set(names)
    results = []
    for name in ABSENT_TOOLS:
        results.append(Result(f"tool {name} absent", name not in listed, "still listed"))
    for name in PRESENT_TOOLS:
        results.append(Result(f"tool {name} present", name in listed, "not listed"))
    return results


def check_lobby_default(model: str, effort: str) -> Result:
    ok = (model, effort) == (EXPECTED_MODEL, EXPECTED_EFFORT)
    return Result(
        "lobby default",
        ok,
        f"got {model}/{effort}, want {EXPECTED_MODEL}/{EXPECTED_EFFORT}",
    )


def check_seat_job_env(job: Mapping[str, Any]) -> Result:
    """No container (init or main) of the rendered seat Job may set the effort-level
    env var: it would override the per-seat `--effort` the wrapper passes."""
    spec = job["spec"]["template"]["spec"]
    offenders = [
        container["name"]
        for container in [*spec.get("initContainers", []), *spec.get("containers", [])]
        if any(var.get("name") == FORBIDDEN_SEAT_ENV for var in container.get("env", []))
    ]
    return Result(
        f"seat Job has no {FORBIDDEN_SEAT_ENV}",
        not offenders,
        f"set in container(s): {', '.join(offenders)}",
    )


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret else text


def render(result: Result, secret: str = "") -> str:
    if result.ok:
        return f"PASS {result.name}"
    return redact(f"FAIL {result.name}: {result.detail}", secret)


def run_checks(
    *,
    get_health: Callable[[], tuple[int, Mapping[str, Any]]],
    list_tools: Callable[[], list[str]],
    default_settings: Callable[[], tuple[str, str]],
    seat_job: Callable[[], Mapping[str, Any]],
    secret: str = "",
    out: Callable[[str], None] = print,
) -> int:
    """Run every check, print one line each, return the process exit status.

    A check whose probe raises is a FAIL with the (scrubbed) error, never a
    crash, so one broken probe cannot hide the others."""
    failed = 0

    def report(results: Iterable[Result]) -> None:
        nonlocal failed
        for result in results:
            out(render(result, secret))
            failed += not result.ok

    def probe(name: str, fn: Callable[[], Iterable[Result]]) -> None:
        try:
            results = list(fn())
        except Exception as exc:
            results = [Result(name, False, f"{type(exc).__name__}: {exc}")]
        report(results)

    probe("health", lambda: [check_health(*get_health())])
    probe("sim tool list", lambda: check_tools(list_tools()))
    probe("lobby default", lambda: [check_lobby_default(*default_settings())])
    probe("seat Job env", lambda: [check_seat_job_env(seat_job())])
    out("smoke: FAILED" if failed else "smoke: all checks passed")
    return 1 if failed else 0


# -- real probes (in-pod only) ------------------------------------------------


def _http_get_health() -> tuple[int, Mapping[str, Any]]:
    with urllib.request.urlopen(BASE_URL + HEALTH_PATH, timeout=10) as response:
        return response.status, json.loads(response.read() or b"{}")


def _mcp_message(response_body: bytes, content_type: str) -> dict[str, Any]:
    """The JSON-RPC message in a streamable-HTTP reply (plain JSON or one SSE event)."""
    text = response_body.decode()
    if "text/event-stream" in content_type:
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else "{}"
    return json.loads(text or "{}")


def mcp_list_tools(token: str, base_url: str = BASE_URL) -> list[str]:
    """`initialize` then `tools/list` against the sim MCP endpoint; the tool names."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def post(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        request = urllib.request.Request(
            base_url + MCP_PATH, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            message = _mcp_message(body, response.headers.get("content-type", "")) if body else {}
            return message, response.headers.get("mcp-session-id")

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "smoke-deploy", "version": "1"},
        },
    }
    _, session = post(init)
    if session:
        headers["mcp-session-id"] = session
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    message, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    return [tool["name"] for tool in message["result"]["tools"]]


def _default_settings() -> tuple[str, str]:
    from magic_cards_model_api.agent_settings import AgentSettings

    settings = AgentSettings.default(os.environ)
    return settings.model, settings.effort


def _seat_job() -> Mapping[str, Any]:
    from magic_cards_model_api import k8s_seats
    from magic_cards_model_api.agent_settings import AgentSettings

    return k8s_seats.build_job(
        k8s_seats.K8sSettings.from_env(os.environ),
        game_id="smoke0000game",
        uid="smoke0000uid",
        player="a",
        agent=AgentSettings.default(os.environ),
    )


def main() -> int:
    token = os.environ.get("MTG_SIM_TOKEN", "")

    def list_tools() -> list[str]:
        if not token:
            raise RuntimeError("MTG_SIM_TOKEN is not set in this container")
        return mcp_list_tools(token)

    return run_checks(
        get_health=_http_get_health,
        list_tools=list_tools,
        default_settings=_default_settings,
        seat_job=_seat_job,
        secret=token,
    )


if __name__ == "__main__":
    sys.exit(main())
