"""Sim worker child process: hosts one SandboxGame over JSON-lines on stdin/stdout.

Run as `python -m magic_cards_model_api.sim.worker` with the environment from
`worker_env`. stdout carries only protocol lines; everything else goes to stderr.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

_STRIPPED_EXACT = frozenset(
    {
        "DATABASE_URL",
        "REQUIRE_POSTGRES",
        "OPS_TOKEN",
        "MTG_SIM_TOKEN",
        "AI_SEAT_MODE",
        "MCM_TEST_DATABASE_URL",
    }
)
_STRIPPED_PREFIXES = ("POSTGRES_", "OIDC_")

EXIT_NOT_IN_MEMORY = 3
EXIT_OVER_RSS = 4


def worker_env(env: Mapping[str, str]) -> dict[str, str]:
    """The child's environment: no database, token or auth settings, plus the worker marker."""
    out = {
        k: v
        for k, v in env.items()
        if k not in _STRIPPED_EXACT and not k.startswith(_STRIPPED_PREFIXES)
    }
    out["MTG_SIM_WORKER"] = "1"
    return out


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux


def _emit(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, separators=(",", ":")) + "\n")
    out.flush()


def _check_rss(out: TextIO, limit_mb: int) -> None:
    if _rss_mb() > limit_mb:
        _emit(
            out,
            {
                "id": None,
                "ok": False,
                "error": f"worker exceeded MTG_SIM_WORKER_MAX_RSS_MB={limit_mb}",
            },
        )
        sys.exit(EXIT_OVER_RSS)


def main() -> None:
    # Keep the real stdout for the protocol; stray prints from libraries go to stderr.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    sys.stdout = sys.stderr
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass

    from fastapi.testclient import TestClient

    from magic_cards_model_api.main import app
    from magic_cards_model_api.repository import InMemoryRepository
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.sandbox_game import SandboxGame
    from magic_cards_model_api.store import store

    if not isinstance(store, InMemoryRepository):
        print("sim worker refuses to run on a non in-memory store", file=sys.stderr)
        sys.exit(EXIT_NOT_IN_MEMORY)

    limit_mb = SimLimits.from_env().worker_max_rss_mb
    test_ops = os.environ.get("MTG_SIM_TEST_OPS") == "1"
    game = SandboxGame(TestClient(app))

    def handle(op: str, args: dict[str, Any]) -> Any:
        if op == "ping":
            return {"pong": True}
        if op == "new":
            try:
                return game.create(args)
            except BaseException:
                game.close()
                raise
        if op == "state":
            return game.state(full=bool(args.get("full", False)))
        if op == "legal_actions":
            return game.legal_actions(args["player"])
        if op == "act":
            return game.act(
                args["action"],
                args.get("player"),
                args.get("body") or {},
                bool(args.get("dry_run", False)),
            )
        if op == "end":
            game.close()
            return {"ended": True}
        if op == "sleep" and test_ops:
            time.sleep(float(args["seconds"]))
            return {"slept": True}
        raise ValueError(f"unknown op {op!r}")

    # A cap below the post-import baseline is unusable: say so before serving.
    _check_rss(out, limit_mb)

    for line in sys.stdin:
        if not line.strip():
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req["id"]
            reply: dict[str, Any] = {
                "id": req_id,
                "ok": True,
                "result": handle(req["op"], req.get("args") or {}),
            }
        except Exception as exc:  # noqa: BLE001 - every failure becomes an ok:false reply
            reply = {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _emit(out, reply)
        if reply["ok"] and req.get("op") == "end":
            return
        _check_rss(out, limit_mb)


if __name__ == "__main__":
    main()
