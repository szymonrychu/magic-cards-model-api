"""Sim worker child process: hosts one SandboxGame over JSON-lines on stdin/stdout.

Run as `python -m magic_cards_model_api.sim.worker` with the environment from
`worker_env`. stdout carries only protocol lines; everything else goes to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
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


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
_WATCHDOG_INTERVAL_S = 0.5
_emit_lock = threading.Lock()


def _rss_mb() -> float:
    """Current resident set size (not the peak), from /proc/self/statm."""
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * _PAGE_SIZE / (1024 * 1024)


def _emit(out: TextIO, payload: dict[str, Any]) -> None:
    with _emit_lock:
        out.write(json.dumps(payload, separators=(",", ":")) + "\n")
        out.flush()


def _over_rss(out: TextIO, limit_mb: int) -> bool:
    """Report the cap breach on the protocol pipe when current RSS is above the cap."""
    if _rss_mb() <= limit_mb:
        return False
    _emit(
        out,
        {
            "id": None,
            "ok": False,
            "error": f"worker exceeded MTG_SIM_WORKER_MAX_RSS_MB={limit_mb}",
        },
    )
    return True


def _watchdog(out: TextIO, limit_mb: int) -> None:
    """Enforce the cap during a call too: one runaway request must not OOM the shared pod."""
    while True:
        time.sleep(_WATCHDOG_INTERVAL_S)
        if _over_rss(out, limit_mb):
            os._exit(EXIT_OVER_RSS)


_LOOKUP_SHARE = 0.5  # of the op budget, for card type lookups when the scenario also runs


def _import_mtgo(game: Any, args: dict[str, Any], max_steps: int) -> dict[str, Any]:
    """Convert an MTGO log (card types need the card data, so this runs here); `run` also
    plays the scenario in this worker's sandbox, under what is left of `budget_s`."""
    from magic_cards_model_api.sim.mtgo_convert import import_log
    from magic_cards_model_api.sim.redact import support_block
    from magic_cards_model_api.sim.scenario import run_scenario, validate_scenario

    deadline = time.monotonic() + float(args["budget_s"])
    run = bool(args.get("run", False))
    share = _LOOKUP_SHARE if run else 1.0
    answer, resolved = import_log(
        args["text"],
        args.get("upto_turn"),
        lookup_timeout_s=float(args["budget_s"]) * share,
        player=args.get("player"),
    )
    answer.update(support_block(resolved))  # resolved names are cached: no network wait
    if not run:
        return answer
    try:
        scenario = validate_scenario(answer["scenario"], max_steps)
        answer["run"] = run_scenario(game, scenario, deadline)
    except Exception as exc:  # noqa: BLE001 - the conversion stands even when the run fails
        game.close()
        answer["run"] = {"error": f"the scenario could not run: {type(exc).__name__}: {exc}"}
    return answer


def main() -> None:
    # Keep the real stdout for the protocol; stray prints from libraries go to stderr.
    # fd 1 is then pointed at stderr so native writes and grandchildren cannot pollute it.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
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
    game = SandboxGame(TestClient(app), max_snapshots=SimLimits.from_env().max_snapshots)

    def _play_budget(args: dict[str, Any]) -> float:
        return float(args.get("budget_s", SimLimits.from_env().call_timeout_s - 3))

    def handle(op: str, args: dict[str, Any], req_id: Any = None) -> Any:
        if op == "ping":
            return {"pong": True}
        if op == "new":
            try:
                return game.create(args)
            except BaseException:
                game.close()
                raise
        if op == "play_new":
            try:
                made = game.create_play(
                    args["deck_me"],
                    args["deck_opponent"],
                    seed=args.get("seed"),
                    me_first=args.get("me_first"),
                )
                return game.settle_play(made, _play_budget(args))
            except BaseException:
                game.close()
                raise
        if op == "run_script":
            from magic_cards_model_api.sim.scenario import run_scenario, validate_scenario

            scenario = validate_scenario(args["script"], SimLimits.from_env().max_script_steps)
            try:
                return run_scenario(game, scenario, time.monotonic() + float(args["budget_s"]))
            except BaseException:
                game.close()
                raise
        if op == "import_mtgo":
            return _import_mtgo(game, args, SimLimits.from_env().max_script_steps)
        if op == "match":
            from magic_cards_model_api.sim.match import play_match

            max_steps = int(args["max_steps"]) if args.get("max_steps") else None
            if test_ops and max_steps is None and os.environ.get("MTG_SIM_TEST_MATCH_MAX_STEPS"):
                max_steps = int(os.environ["MTG_SIM_TEST_MATCH_MAX_STEPS"])
            play_match(
                args["deck_a"],
                args["deck_b"],
                int(args["games"]),
                int(args["seed"]),
                lambda row: _emit(out, {"id": req_id, "progress": row}),
                max_steps=max_steps,
            )
            return {"done": True}
        if op == "match_slow" and test_ops:
            row = {"game": 0, "seed": args["seed"], "a_seat": 1, "winner": "a", "drawn": False}
            _emit(out, {"id": req_id, "progress": row})
            time.sleep(float(args.get("seconds", 600)))
            return {"done": True}
        if op == "state":
            board = game.state(full=bool(args.get("full", False)))
            return game.settle_play(board, _play_budget(args)) if game.play_mode else board
        if op == "legal_actions":
            return game.legal_actions(args["player"])
        if op == "act":
            done = game.act(
                args["action"],
                args.get("player"),
                args.get("body") or {},
                bool(args.get("dry_run", False)),
            )
            if not game.play_mode:
                return done
            # After an accepted action the baseline plays on until it is the chat's move.
            accepted = done["result"].get("allowed") is not False and not args.get("dry_run")
            return game.settle_play(done, _play_budget(args), advance=accepted)
        if op == "snapshot":
            return game.snapshot(args.get("label"))
        if op == "restore":
            return game.restore(args["snapshot"])
        if op == "end":
            game.close()
            return {"ended": True}
        if op == "sleep" and test_ops:
            time.sleep(float(args["seconds"]))
            return {"slept": True}
        if op == "alloc" and test_ops:
            hog = b"x" * (int(args["mb"]) * 1024 * 1024)  # touched, so resident
            time.sleep(float(args.get("seconds", 30)))
            return {"alloc": len(hog)}
        raise ValueError(f"unknown op {op!r}")

    # A cap below the post-import baseline is unusable: say so before serving.
    if _over_rss(out, limit_mb):
        sys.exit(EXIT_OVER_RSS)
    threading.Thread(target=_watchdog, args=(out, limit_mb), daemon=True).start()

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
                "result": handle(req["op"], req.get("args") or {}, req_id),
            }
        except Exception as exc:  # noqa: BLE001 - every failure becomes an ok:false reply
            reply = {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _emit(out, reply)
        if reply["ok"] and req.get("op") == "end":
            return


if __name__ == "__main__":
    main()
