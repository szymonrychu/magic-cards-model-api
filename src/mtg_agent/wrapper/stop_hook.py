"""The Stop hook claude runs at end-of-turn.

It reads the hook payload on stdin, builds the turn result, and POSTs it to
the wrapper's loopback-only internal endpoint. `last_assistant_message` from
the payload is authoritative for the final text; the transcript is read for
usage, stop_reason, per-turn token totals, and as a text fallback.

It ALWAYS exits 0. A hook that fails loudly blocks or alters claude, which is
never worth it - a lost result is recoverable, a wedged agent is not.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from .transcript import last_assistant, last_turn_marker, pending_background_tasks, sum_turn_tokens

MAX_POST_ATTEMPTS = 5
POST_PER_ATTEMPT = 5.0
POST_BACKOFF = 0.5

log = logging.getLogger("cc-stop-hook")


def build_result(payload: bytes, result_json_path: Path) -> dict:
    parsed = json.loads(payload)
    transcript_path = parsed.get("transcript_path") or ""
    result = {
        "sessionId": parsed.get("session_id") or "",
        "finalText": parsed.get("last_assistant_message") or "",
        "transcriptPath": transcript_path,
        "usage": None,
        "stopReason": "",
        "turnTokens": [],
        "resultJson": None,
        #: Which turn stopped, read off the transcript (`last_turn_marker`);
        #: "" when it names none. `Manager.complete` ignores a stale one.
        "turnId": "",
        #: The open-turn work the turn launched that has not ended
        #: (`pending_background_tasks`): subagents, teammates and background
        #: commands, but not a seat pair's `future-N` helper. While any is
        #: listed, `Manager.complete` keeps the turn in flight.
        "pendingTasks": [],
    }
    if transcript_path:
        path = Path(transcript_path)
        result["turnId"] = last_turn_marker(path)
        result["pendingTasks"] = pending_background_tasks(path)
        text, usage, stop = last_assistant(path)
        result["usage"] = usage
        result["stopReason"] = stop
        if not result["finalText"]:
            result["finalText"] = text
        result["turnTokens"] = [vars(t) for t in sum_turn_tokens(path)]
    with contextlib.suppress(OSError, ValueError):
        result["resultJson"] = json.loads(Path(result_json_path).read_text())
    return result


def _post_once(url: str, content: bytes, headers: dict, timeout: float) -> None:
    response = httpx.post(url, content=content, headers=headers, timeout=timeout)
    response.raise_for_status()


def post_with_retry(
    url: str,
    body: bytes,
    *,
    attempts: int = MAX_POST_ATTEMPTS,
    backoff: float = POST_BACKOFF,
    timeout: float = POST_PER_ATTEMPT,
) -> None:
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(backoff)
        try:
            _post_once(url, body, {"Content-Type": "application/json"}, timeout)
            return
        except Exception as exc:  # noqa: BLE001 - every failure mode retries alike
            last = exc
    raise RuntimeError(f"post to {url} failed after {attempts} attempts: {last}")


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        payload = sys.stdin.buffer.read()
        url = os.environ.get("CCW_INTERNAL_URL", "http://127.0.0.1:8090/internal/turn-complete")
        result_path = Path(os.environ.get("CCW_RESULT_JSON", "/workspace/result.json"))
        result = build_result(payload, result_path)
        post_with_retry(url, json.dumps(result).encode())
        log.info("hook post succeeded session_id=%s", result["sessionId"])
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.error("cc-stop-hook failed: %s", exc)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
