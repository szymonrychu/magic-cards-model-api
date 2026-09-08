"""The play UI has to say something while its opponent's AI seat is still
booting, instead of the silent dead air a human otherwise stares at.

Kubernetes gives a seat's own pod tens of seconds before it can act at all -
image pull, two initContainers (`guides`, `mcp-config`), the `agent`
container's own `claude` CLI boot (measured `boot_ms` around 5000), then the
`driver` dispatching the first turn - and until this resolves the other
player had nothing on screen that said whether their opponent was coming,
stuck, or dead.

This runs `aiSeatLoadingNotice` out of the real `ui/store.js` under Node, the
same black-box way `test_ui_ai_stall.py` runs `aiStallNotice` - no Python
restatement of the JS anywhere here. `aiStatus` is `ai_seat.registry.status`
verbatim (the same payload `GET .../ai-seat/status` and `GET .../seats` both
hand back - see `decks_api.py`'s `SeatDetail.ai_seat` and `ai_seat_api.py`'s
`ai_seat_status`), across every mechanism (`subprocess`, `wrapper`,
`kubernetes`) and every state each one reports (`ai_seat.py`,
`k8s_seats.py`).
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def notice(ai_uid: str | None, ai_status: dict[str, Any] | None) -> Any:
    import json

    return run_module(
        "store.js",
        f"m.aiSeatLoadingNotice({json.dumps(ai_uid)}, {json.dumps(ai_status)})",
    )


# ------------------------------------------------------- no AI seat at all --


def test_a_human_vs_human_game_gets_no_notice_at_all():
    assert notice(None, None) is None


def test_no_notice_once_the_ai_uid_is_known_but_nothing_has_been_polled_yet():
    """Right after game creation `ai-seat/start` is fired without being
    awaited (`ui/render/lobby.js`) - the very first tick or two can have an
    `aiUid` with no status payload behind it yet. That is still the ordinary
    boot window, not a failure."""
    assert notice("ai-uid", None) == {"failed": False, "message": "starting the agent"}


# ------------------------------------------------------------------ pending --


def test_kubernetes_pod_not_yet_scheduled_is_starting_the_agent():
    status = {
        "uid": "ai-uid",
        "mechanism": "kubernetes",
        "state": "pending",
        "reason": None,
        "pod": None,
    }
    assert notice("ai-uid", status) == {"failed": False, "message": "starting the agent"}


def test_kubernetes_pod_running_but_driver_not_yet_up_says_more_than_a_bare_spinner():
    """Distinguishable from bare `pending` using only fields the endpoint
    already reports (`pod`) - not an invented phase: the pod is booted, the
    driver just has not answered yet (still inside the ~5s `claude` boot, or
    the driver container's own startup)."""
    status = {
        "uid": "ai-uid",
        "mechanism": "kubernetes",
        "state": "pending",
        "reason": "Running",
        "pod": "Running",
    }
    result = notice("ai-uid", status)
    assert result["failed"] is False
    assert result["message"] != "starting the agent"


def test_kubernetes_driver_reachable_and_still_booting_the_agent_container():
    status = {
        "uid": "ai-uid",
        "mechanism": "kubernetes",
        "state": "pending",
        "reason": None,
        "pod": "Running",
        "driver": {"pending": True},
    }
    result = notice("ai-uid", status)
    assert result["failed"] is False


def test_wrapper_dispatch_still_pending_is_loading():
    status = {"uid": "ai-uid", "mechanism": "wrapper", "state": "pending", "player": "claude"}
    assert notice("ai-uid", status) == {"failed": False, "message": "starting the agent"}


def test_subprocess_starting_before_the_process_is_confirmed_running():
    status = {"uid": "ai-uid", "mechanism": "subprocess", "state": "starting"}
    assert notice("ai-uid", status) == {"failed": False, "message": "starting the agent"}


def test_not_started_with_no_mechanism_yet_is_still_loading():
    status = {"uid": "ai-uid", "mechanism": None, "state": "not_started"}
    assert notice("ai-uid", status) == {"failed": False, "message": "starting the agent"}


# --------------------------------------------------------------- running ----


def test_kubernetes_thinking_resolves_the_notice():
    status = {"uid": "ai-uid", "mechanism": "kubernetes", "state": "thinking", "pod": "Running"}
    assert notice("ai-uid", status) is None


def test_kubernetes_idle_resolves_the_notice():
    """Pod up, driver reachable, nothing owed right now - the boot gap this
    notice exists for is over even though the agent has not taken an action
    this instant."""
    status = {"uid": "ai-uid", "mechanism": "kubernetes", "state": "idle", "pod": "Running"}
    assert notice("ai-uid", status) is None


def test_wrapper_dispatched_resolves_the_notice():
    status = {"uid": "ai-uid", "mechanism": "wrapper", "state": "dispatched"}
    assert notice("ai-uid", status) is None


def test_subprocess_thinking_resolves_almost_instantly():
    """A local `docker compose`/subprocess seat has no pod and comes up
    almost instantly - the loading state must not stick around there."""
    status = {"uid": "ai-uid", "mechanism": "subprocess", "state": "thinking"}
    assert notice("ai-uid", status) is None


def test_subprocess_stopped_is_not_a_failure():
    """The seat played its match through and its one-shot process exited
    cleanly - not a boot failure, nothing left to report."""
    status = {"uid": "ai-uid", "mechanism": "subprocess", "state": "stopped", "returncode": 0}
    assert notice("ai-uid", status) is None


# ---------------------------------------------------------------- failure ---


def test_kubernetes_image_pull_failure_is_reported_distinctly():
    status = {
        "uid": "ai-uid",
        "mechanism": "kubernetes",
        "state": "error",
        "reason": "ImagePullBackOff",
    }
    result = notice("ai-uid", status)
    assert result["failed"] is True
    assert "ImagePullBackOff" in result["message"]


def test_kubernetes_driver_grace_backstop_firing_is_reported_distinctly():
    """`k8s_seats.py`'s `driver_grace_seconds` backstop: a `Running` pod past
    grace with an unreachable driver is a dead driver container, not "still
    booting" - `state: "error"`, `reason: "driver unreachable"`."""
    status = {
        "uid": "ai-uid",
        "mechanism": "kubernetes",
        "state": "error",
        "reason": "driver unreachable",
    }
    result = notice("ai-uid", status)
    assert result["failed"] is True
    assert "driver unreachable" in result["message"]


def test_kubernetes_job_failing_is_distinguished_from_a_clean_finish():
    """`k8s_seats.py`'s `status()` reports BOTH a clean finish and a hard pod
    failure as `state: "gone"` - only `reason` (the pod phase) tells them
    apart. A failure must never be dressed up as still loading, or as an
    ordinary, unremarkable end."""
    failed = {"uid": "ai-uid", "mechanism": "kubernetes", "state": "gone", "reason": "Failed"}
    result = notice("ai-uid", failed)
    assert result["failed"] is True

    clean = {
        "uid": "ai-uid",
        "mechanism": "kubernetes",
        "state": "gone",
        "reason": "the Job was cleaned up",
    }
    assert notice("ai-uid", clean) is None


def test_subprocess_error_is_reported_distinctly():
    status = {"uid": "ai-uid", "mechanism": "subprocess", "state": "error", "returncode": 1}
    result = notice("ai-uid", status)
    assert result["failed"] is True


# ------------------------------------------------ endpoint erroring/unreachable


def test_wrapper_driver_unreachable_is_reported_as_a_failure_not_a_loading_state():
    """`ai_seat.py`'s `_live_driver_status` reports `state: "unreachable"`
    when the game server itself cannot reach the driver at all - a real,
    backend-reported state, not a guess."""
    status = {"uid": "ai-uid", "mechanism": "wrapper", "state": "unreachable"}
    result = notice("ai-uid", status)
    assert result["failed"] is True


def test_a_poll_that_never_landed_is_treated_as_still_loading_not_failed():
    """`pollSeats` (`ui/app.js`) swallows its own fetch failures and leaves
    `aiStatus` exactly as it was - so a status payload that never arrived at
    all must not be dressed up as a definite failure. It stays the honest
    "still loading" default and the next poll tries again."""
    assert notice("ai-uid", None) == {"failed": False, "message": "starting the agent"}


# --------------------------------------------------------- fallback mechanism


def test_fallback_gets_no_notice_of_its_own():
    """`mechanism: "fallback"` already has its own distinct panel and "run it
    by hand" button (`renderAiPanel`) - a second banner saying the same thing
    in a different corner of the screen would only be confusing."""
    status = {
        "uid": "ai-uid",
        "mechanism": "fallback",
        "state": "not_started",
        "reason": "the `claude` CLI is not on PATH",
    }
    assert notice("ai-uid", status) is None
