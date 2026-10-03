"""Probe: can the seat's PTY claude run a pair of Opus helpers, and over which channel?

A manual spike harness, NOT part of the test suite (`mise run test` never
reaches it): it builds the real agent image (`Dockerfile.agent`, claude pinned
to 2.1.282), starts the single-seat compose stack's `game` + `agent` containers
under a compose project name of its own, and pushes ONE probe turn through the
wrapper API (`POST /v1/messages`, then `GET /v1/messages/{id}` until it ends) -
the real interactive claude under the PTY, bypassPermissions, the Stop hook,
no tmux. It costs real API time: a Sonnet lead plus two short Opus helpers.

    mise run probe:seat-pair -- --channel teams
    mise run probe:seat-pair -- --channel subagents

`teams` turns on agent teams for this container only
(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, `teammateMode: in-process`, merged
into the container's `~/.claude/settings.json` and picked up by restarting the
supervised claude through `POST /v1/session`). `subagents` uses the stock
Agent tool with `run_in_background`, `SendMessage` and `TaskStop`.

The verdict is read from the transcripts on disk, never from what the lead
says it did. The probe writes `report.json` (per check: pass/fail + one
evidence line) under `--out`, copies every transcript (main session, each
helper's, with their paths relative to `~/.claude/projects`) redacted of keys
into `tests/wrapper/fixtures/seat_pair/<channel>/`, tears the stack down and
exits non-zero if any check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "wrapper" / "fixtures" / "seat_pair"
CLAUDE_VERSION = "2.1.282"
HOME = "/agent-home"
LEAD_MODEL = "claude-sonnet-5"
HELPER_MODEL = "opus"

TEAMS_SETTINGS = {
    "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
    "teammateMode": "in-process",
}

# The game service's healthcheck and the agent's `depends_on: game` are kept;
# everything that would collide with another stack (fixed host ports), needs
# credentials this probe has no use for (the guides clone and its PAT), or
# writes into the checkout (`./memory`) is dropped.
OVERRIDE_YAML = """\
services:
  game:
    build:
      context: {context}
    ports: !reset []
    volumes: !override
      - scryfall-cache:/app/.scryfall-cache
  agent:
    build:
      context: {context}
    ports: !reset []
    depends_on: !override
      game:
        condition: service_healthy
    volumes: !reset []
    secrets: !reset []
    restart: "no"
    stop_grace_period: 10s
    environment:
      - MODEL={lead_model}
      - EFFORT={effort}
      - AGENT_LOG_LEVEL=tools
"""


@dataclass
class Check:
    name: str
    passed: bool
    evidence: str


@dataclass
class Report:
    channel: str
    claude_version: str = ""
    turn_state: str = ""
    nonce: str = ""
    secret: str = ""
    turn_completed_at: float = 0.0
    checks: list[Check] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def to_json(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["passed"] = self.passed
        return doc


# --------------------------------------------------------------------------
# prompts


def teams_prompt(nonce: str, secret: str, uid: str) -> str:
    # 2.1.282 has no TeamCreate: "the session has a single implicit team"
    # (`teams/session-<id>/config.json`, lead `team-lead`), Agent's
    # `team_name` is ignored, and every named Agent spawn joins that team.
    return f"""\
This turn is a capability probe of agent teams, not a game. Do not play Magic and \
do not read any game files. Agent teams are on: this session already has a single \
implicit team (there is no TeamCreate tool; you are its `team-lead`). Use the Agent \
tool with `name` and `model` to spawn teammates, SendMessage to talk to them, and \
SendMessage `{{"type": "shutdown_request"}}` or TaskStop to stop one.

1. Spawn teammate `alpha` (Agent tool, name `alpha`, model `{HELPER_MODEL}`, \
subagent_type general-purpose) with this prompt (verbatim): "You are alpha. Your \
secret word is {secret}. Remember it, never say it until the lead asks for it. Do \
these now: (a) call mcp__mtg__battlefield_state with player_uid `{uid}` once; \
(b) call mcp__spellslinger__knowledge_search once with query `priority`; (c) send \
teammate `beta` a direct message (SendMessage to \
`beta`) whose text is exactly `NONCE {nonce}`; (d) send the lead (`team-lead`) the \
message `alpha ready`, then wait for messages. When the lead asks for your secret, \
reply to the lead with `SECRET <your secret word>`."
2. Spawn teammate `beta` the same way (name `beta`, model `{HELPER_MODEL}`) with this \
prompt (verbatim): "You are beta. Do these now: (a) call mcp__mtg__battlefield_state \
with player_uid `{uid}` once; (b) call \
mcp__spellslinger__knowledge_search once with query `stack`; (c) wait for a direct \
message from teammate alpha; when it arrives, send the lead (`team-lead`) a message \
quoting it: `BETA GOT <the exact text alpha sent>`; then wait for messages."
3. Wait until beta's message quoting the nonce reaches you. Then send alpha a \
continuation message with SendMessage to `alpha` (do not spawn a new alpha, and do \
NOT include the secret word): "The lead asks for your secret word now."
4. Wait until alpha's `SECRET ...` reply reaches you.
5. Stop beta (SendMessage to `beta` with a shutdown_request, or TaskStop `beta`).
6. Then stop alpha the same way.
7. End your turn with one final line: `PROBE DONE`.
"""


def subagents_prompt(nonce: str, secret: str, uid: str) -> str:
    return f"""\
This turn is a capability probe of background subagents, not a game. Do not play \
Magic and do not read any game files. Follow these steps in order, using the Agent \
tool with `run_in_background: true`, `name` and `model`, then SendMessage and \
TaskStop. Never use a team.

1. Launch background agent `alpha` (Agent tool, name `alpha`, model \
`{HELPER_MODEL}`, run_in_background true, subagent_type general-purpose) with this \
prompt (verbatim): "You are alpha. Your secret word is {secret}. Remember it, never \
say it until you are asked for it. Do these now: (a) call mcp__mtg__battlefield_state \
with player_uid `{uid}` once; (b) call \
mcp__spellslinger__knowledge_search once with query `priority`; (c) finish with \
exactly: `FOR BETA: NONCE {nonce}`. If you are later asked for your secret, answer \
`SECRET <your secret word>`."
2. Launch background agent `beta` the same way (name `beta`, model `{HELPER_MODEL}`) \
with this prompt (verbatim): "You are beta. Do these now: (a) call \
mcp__mtg__battlefield_state with player_uid `{uid}` once; (b) call \
mcp__spellslinger__knowledge_search once with query \
`stack`; (c) finish with `beta ready`. If you are later sent a message from alpha, \
answer `BETA GOT <the exact text of alpha's message>`. If you are later asked to \
sleep, run the Bash command `sleep 600` and only then answer `slept`."
3. Wait for both completion notifications. Relay alpha's message to beta yourself: \
SendMessage to `beta` with "Message from alpha: <alpha's FOR BETA text>". Wait for \
beta's answer quoting the nonce.
4. Continue alpha (do not launch a new one, and do NOT include the secret word): \
SendMessage to `alpha` with "The lead asks for your secret word now." Wait for its \
`SECRET ...` answer.
5. SendMessage to `beta` with "Please sleep now." As soon as beta is running again, \
stop it with TaskStop and wait for the notification that it stopped.
6. End your turn with one final line: `PROBE DONE`.
"""


# --------------------------------------------------------------------------
# docker plumbing


class Stack:
    def __init__(self, project: str, workdir: Path, env: dict[str, str]):
        self.project = project
        self.override = workdir / "compose.override.yml"
        self.env = env

    def compose(self, *args: str, check: bool = True, capture: bool = False, **kw: Any):
        cmd = [
            "docker",
            "compose",
            "-p",
            self.project,
            "-f",
            str(REPO / "docker-compose.yml"),
            "-f",
            str(self.override),
            *args,
        ]
        return subprocess.run(
            cmd,
            cwd=REPO,
            env=self.env,
            check=check,
            text=True,
            capture_output=capture,
            **kw,
        )

    def sh(self, script: str, stdin: str | None = None, check: bool = True) -> str:
        proc = self.compose(
            "exec", "-T", "agent", "sh", "-c", script, check=False, capture=True, input=stdin
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"agent exec failed ({proc.returncode}): {script}\n{proc.stderr}")
        return proc.stdout

    def api(self, method: str, path: str, body: dict | None = None) -> Any:
        data = "" if body is None else "-H 'Content-Type: application/json' --data-binary @-"
        out = self.sh(
            f"curl -sS -X {method} {data} http://127.0.0.1:8080{path}",
            stdin=None if body is None else json.dumps(body),
        )
        return json.loads(out) if out.strip() else None


def wait_ready(stack: Stack, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        out = stack.sh("curl -s http://127.0.0.1:8080/readyz || true", check=False)
        try:
            doc = json.loads(out)
            if isinstance(doc, dict) and doc.get("state") not in (None, "dead", "booting"):
                return doc
        except ValueError:
            pass
        last = out.strip()
        time.sleep(3)
    raise RuntimeError(f"wrapper never became ready: {last!r}")


def enable_teams(stack: Stack) -> None:
    """Merge the teams switches into the container's home settings only, then
    restart the supervised claude so it reads them (bootstrap ran before)."""
    script = (
        "import json,pathlib,sys;"
        f"p=pathlib.Path('{HOME}/.claude/settings.json');"
        "d=json.loads(p.read_text());"
        "add=json.loads(sys.stdin.read());"
        "d.setdefault('env',{}).update(add['env']);"
        "d['teammateMode']=add['teammateMode'];"
        "p.write_text(json.dumps(d,indent=2)+'\\n')"
    )
    stack.sh(f'python -c "{script}"', stdin=json.dumps(TEAMS_SETTINGS))


def create_seat(stack: Stack) -> str:
    """A real two-player game, so the helpers' battlefield_state call has a
    seat uid the engine knows and answers for (an unknown uid only gets the
    MCP layer's bare "Error executing tool")."""
    out = stack.sh(
        "curl -sS -X POST http://game:8000/api/games -H 'Content-Type: application/json' "
        "--data-binary @-",
        stdin=json.dumps({"players": [{"username": "alice"}, {"username": "bob"}]}),
    )
    return str(json.loads(out)["seat_uids"]["alice"])


#: Run inside the agent container: has the lead (a main-session transcript)
#: written an assistant `PROBE DONE`, how long since anything under
#: ~/.claude/projects changed, and the team configs as they are right now.
SETTLE_SCRIPT = f"""
import glob, json, os, time
root = '{HOME}/.claude'
done = False
for path in glob.glob(root + '/projects/*/*.jsonl'):
    for line in open(path):
        if '"assistant"' in line and 'PROBE DONE' in line:
            done = True
files = glob.glob(root + '/projects/**/*', recursive=True)
newest = max((os.path.getmtime(f) for f in files if os.path.isfile(f)), default=0)
teams = []
for path in glob.glob(root + '/teams/*/config.json'):
    try:
        teams.append(json.load(open(path)))
    except ValueError:
        pass
print(json.dumps({{"done": done, "quiet": time.time() - newest, "teams": teams}}))
"""


def wait_settled(stack: Stack, budget: float, peak_teams: dict[str, dict]) -> str:
    """Until the lead has written `PROBE DONE` and nothing under
    ~/.claude/projects has changed for 20s, or the budget runs out. Every
    poll keeps the fullest snapshot of each team config: a teammate that
    shuts down is removed from `config.json`, so the end state alone cannot
    show who was on the team."""
    deadline = time.monotonic() + max(budget, 30.0)
    while time.monotonic() < deadline:
        try:
            state = json.loads(stack.sh("python -", stdin=SETTLE_SCRIPT))
        except (RuntimeError, ValueError):
            time.sleep(5)
            continue
        for team in state["teams"]:
            name = str(team.get("name"))
            if len(team.get("members", [])) >= len(peak_teams.get(name, {}).get("members", [])):
                peak_teams[name] = team
        if state["done"] and state["quiet"] >= 20:
            return f"lead wrote PROBE DONE; transcripts quiet for {state['quiet']:.0f}s"
        time.sleep(5)
    return "budget spent before the lead finished"


def run_turn(stack: Stack, text: str, timeout: float) -> dict:
    turn = stack.api("POST", "/v1/messages", {"text": text})
    turn_id = turn["turnId"]
    print(f"probe: turn {turn_id} submitted", flush=True)
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        record = stack.api("GET", f"/v1/messages/{turn_id}") or {}
        if record.get("state") not in ("running", "TurnState.RUNNING", None):
            return record
        time.sleep(5)
    record["state"] = f"probe-timeout ({record.get('state')})"
    return record


# --------------------------------------------------------------------------
# transcripts


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def blocks(row: dict) -> list[dict]:
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_of(v) for v in value)
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        if "content" in value:
            return text_of(value["content"])
        return json.dumps(value)
    return "" if value is None else str(value)


@dataclass
class Transcript:
    rel: str
    rows: list[dict]
    #: The sibling `<agent>.meta.json` a helper transcript has (name,
    #: teamName, taskKind, model); empty for the main session.
    meta: dict = field(default_factory=dict)

    @property
    def is_main(self) -> bool:
        return not any(r.get("isSidechain") for r in self.rows) and "/subagents/" not in self.rel

    def ident(self) -> dict[str, Any]:
        """The identity fields a transcript carries, first value seen."""
        keys = ("agentId", "teamName", "agentName", "name", "isSidechain", "sessionId")
        out: dict[str, Any] = {
            k: self.meta[k]
            for k in ("name", "teamName", "taskKind", "agentType", "model")
            if k in self.meta
        }
        for row in self.rows:
            for k in keys:
                if k in row and k not in out:
                    out[k] = row[k]
            model = (row.get("message") or {}).get("model")
            if model and "apiModel" not in out:
                out["apiModel"] = model
        return out

    def tool_uses(self, name_re: str) -> list[dict]:
        pat = re.compile(name_re)
        return [
            b
            for r in self.rows
            if r.get("type") == "assistant"
            for b in blocks(r)
            if b.get("type") == "tool_use" and pat.search(str(b.get("name")))
        ]

    def tool_result(self, tool_use_id: str) -> dict | None:
        for r in self.rows:
            for b in blocks(r):
                if b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id:
                    return b
        return None

    def text(self) -> str:
        return "\n".join(json.dumps(r) for r in self.rows)

    def delivered(self) -> list[dict]:
        """Rows that carry something into this session's context: user rows,
        and the `queued_command` attachments a message or task-notification
        arrives as when it lands mid-turn. The submitted probe prompt itself
        is excluded, since it quotes every marker the checks look for."""
        out = []
        for r in self.rows:
            if r.get("type") == "user":
                if "capability probe" in json.dumps(r):
                    continue
                out.append(r)
            elif (r.get("attachment") or {}).get("type") == "queued_command":
                out.append(r)
        return out

    def user_texts(self) -> list[str]:
        out = []
        for r in self.rows:
            if r.get("type") != "user":
                continue
            for b in blocks(r):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
        return out


def team_configs(raw: Path) -> list[dict]:
    out = []
    for path in sorted((raw / "teams").glob("*/config.json")):
        try:
            out.append(json.loads(path.read_text()))
        except ValueError:
            continue
    return out


def collect(stack: Stack, dest: Path) -> list[Transcript]:
    """Copy ~/.claude/{projects,teams,tasks} out of the container."""
    dest.mkdir(parents=True, exist_ok=True)
    cid = stack.compose("ps", "-q", "agent", capture=True).stdout.strip()
    for sub in ("projects", "teams", "tasks"):
        subprocess.run(
            ["docker", "cp", f"{cid}:{HOME}/.claude/{sub}", str(dest / sub)],
            check=False,
            capture_output=True,
        )
    out = []
    projects = dest / "projects"
    for path in sorted(projects.rglob("*.jsonl")) if projects.is_dir() else []:
        meta_path = path.with_name(path.stem + ".meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        out.append(Transcript(str(path.relative_to(projects)), load_jsonl(path), meta))
    return out


# --------------------------------------------------------------------------
# checks


def _epoch(iso: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat() if epoch else "never"


def _short(s: str, n: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def helper_transcripts(ts: list[Transcript], name: str) -> list[Transcript]:
    """Every helper transcript that belongs to `name` - matched by its
    identity fields, else by its first prompt naming it (`You are alpha`)."""
    found = []
    for t in ts:
        if t.is_main:
            continue
        ident = t.ident()
        if name in (ident.get("agentName"), ident.get("name")):
            found.append(t)
            continue
        first = next(iter(t.user_texts()), "")
        if not first:
            first = text_of((t.rows[0].get("message") or {}).get("content")) if t.rows else ""
        if f"You are {name}." in first:
            found.append(t)
    return found


def mcp_check(helper: str, ts: list[Transcript], tool: str) -> Check:
    name = f"{helper}_calls_{tool.split('__')[1]}"
    for t in ts:
        for use in t.tool_uses(re.escape(tool) + "$"):
            res = t.tool_result(use.get("id", ""))
            if res is None:
                return Check(name, False, f"{t.rel}: {tool} called, no result")
            body = text_of(res.get("content"))
            if "No such tool" in body or "not available" in body.lower():
                return Check(name, False, f"{t.rel}: {_short(body)}")
            return Check(name, True, f"{t.rel}: {tool} -> {_short(body, 140)}")
    return Check(name, False, f"no {tool} call in any {helper} transcript")


def model_check(helper: str, ts: list[Transcript]) -> Check:
    if not ts:
        return Check(f"{helper}_spawned_on_opus", False, f"no transcript for {helper}")
    models = {t.ident().get("apiModel") for t in ts}
    ok = all(m and "opus" in m for m in models)
    return Check(
        f"{helper}_spawned_on_opus",
        ok,
        f"{', '.join(t.rel for t in ts)} model={sorted(str(m) for m in models)}",
    )


def single_session_check(helper: str, ts: list[Transcript]) -> Check:
    """Continue-not-respawn: one transcript (one agentId) for the helper."""
    ids = {t.ident().get("agentId") for t in ts}
    return Check(
        f"{helper}_continued_not_respawned",
        len(ts) == 1 and len(ids) == 1,
        f"{len(ts)} transcript(s), agentIds={sorted(str(i) for i in ids)}",
    )


def team_check(teams: list[dict]) -> Check:
    """The implicit team (no TeamCreate in 2.1.282) must list both helpers
    as in-process members beside the lead."""
    for team in teams:
        members = {m.get("name"): m for m in team.get("members", [])}
        if {"alpha", "beta"} <= members.keys():
            kinds = sorted({str(m.get("backendType")) for m in members.values()})
            models = {n: members[n].get("model") for n in ("alpha", "beta")}
            return Check(
                "team_created",
                all(k == "in-process" for k in kinds),
                f"teams/{team.get('name')}/config.json members={sorted(members)} "
                f"backend={kinds} models={models} (implicit team; no TeamCreate tool)",
            )
    names = [sorted(m.get("name") for m in t.get("members", [])) for t in teams]
    return Check("team_created", False, f"no team config lists alpha+beta: {names}")


def analyse(
    report: Report,
    ts: list[Transcript],
    wrapper_log: str,
    teams: list[dict],
    end_teams: list[dict] | None = None,
) -> None:
    nonce, secret = report.nonce, report.secret
    mains = [t for t in ts if t.is_main]
    lead = max(mains, key=lambda t: len(t.rows)) if mains else None
    report.notes["transcripts"] = {t.rel: t.ident() for t in ts}
    report.checks.append(
        Check(
            "turn_completed",
            report.turn_state in ("complete", "TurnState.COMPLETE"),
            f"wrapper turn state={report.turn_state}",
        )
    )
    if lead is None:
        report.checks.append(Check("lead_transcript", False, "no main-session transcript"))
        return
    if report.channel == "teams":
        report.checks.append(team_check(teams))
    spawns = lead.tool_uses(r"^(Agent|Task)$")
    report.notes["lead_spawns"] = [
        {k: v for k, v in (u.get("input") or {}).items() if k != "prompt"} for u in spawns
    ]
    alpha, beta = helper_transcripts(ts, "alpha"), helper_transcripts(ts, "beta")
    for name, helper in (("alpha", alpha), ("beta", beta)):
        report.checks.append(model_check(name, helper))
        report.checks.append(mcp_check(name, helper, "mcp__mtg__battlefield_state"))
        report.checks.append(mcp_check(name, helper, "mcp__spellslinger__knowledge_search"))

    # The nonce travels alpha -> beta (directly for teams, via the lead for
    # subagents), and beta's quote of it has to reach the lead.
    if report.channel == "teams":
        sends = [
            u
            for t in alpha
            for u in t.tool_uses(r"^SendMessage$")
            if nonce in json.dumps(u.get("input")) and "beta" in json.dumps(u.get("input")).lower()
        ]
        report.checks.append(
            Check(
                "alpha_messages_beta_directly",
                bool(sends),
                f"alpha SendMessage {_short(json.dumps(sends[0].get('input')), 160)}"
                if sends
                else "no SendMessage to beta carrying the nonce in alpha's transcript",
            )
        )
    else:
        relays = [
            u
            for u in lead.tool_uses(r"^SendMessage$")
            if nonce in json.dumps(u.get("input")) and "beta" in json.dumps(u.get("input")).lower()
        ]
        report.checks.append(
            Check(
                "lead_relays_alpha_to_beta",
                bool(relays),
                f"lead SendMessage {_short(json.dumps(relays[0].get('input')), 160)}"
                if relays
                else "no SendMessage to beta carrying the nonce in the lead transcript",
            )
        )
    # "BETA GOT NONCE <n>" (teams) or "BETA GOT FOR BETA: NONCE <n>" (the
    # relayed text, subagents).
    got = re.compile(r"BETA GOT[^<\"\\]*NONCE " + re.escape(nonce))
    quoted = [json.dumps(r) for r in lead.delivered() if got.search(json.dumps(r))]
    report.checks.append(
        Check(
            "beta_quotes_nonce_to_lead",
            bool(quoted),
            f"lead received: {_short(quoted[0], 220)}"
            if quoted
            else "lead never saw BETA GOT + nonce",
        )
    )

    # Continue-not-respawn: the lead's continuation to alpha must not carry
    # the secret, and alpha must answer it from its spawn context.
    conts = [
        u
        for u in lead.tool_uses(r"^SendMessage$")
        if "alpha" in json.dumps(u.get("input")).lower()
        and "secret" in json.dumps(u.get("input")).lower()
    ]
    leaked = [u for u in conts if secret in json.dumps(u.get("input"))]
    echoed = [r for r in lead.delivered() if f"SECRET {secret}" in json.dumps(r)]
    report.checks.append(
        Check(
            "alpha_continuation_keeps_context",
            bool(conts) and not leaked and bool(echoed),
            (
                f"continuation={_short(json.dumps(conts[0].get('input')), 120)}; "
                f"lead received: {_short(json.dumps(echoed[0]), 160)}"
            )
            if conts and echoed and not leaked
            else f"continuations={len(conts)} leaked_secret={len(leaked)} echoes={len(echoed)}",
        )
    )
    report.checks.append(single_session_check("alpha", alpha))

    # Stopping beta.
    # A stop call counts when it succeeded and names beta, by name in its
    # input or by beta's task in its result (a TaskStop on some other task
    # does not stop beta).
    stop_ok: list[tuple[dict, dict]] = []
    for use in lead.tool_uses(r"^(SendMessage|TaskStop)$"):
        if use.get("name") == "SendMessage" and "shutdown_request" not in json.dumps(
            use.get("input")
        ):
            continue
        res = lead.tool_result(use.get("id", ""))
        if res is None or res.get("is_error"):
            continue
        if "beta" in json.dumps(use.get("input")).lower() or "beta" in text_of(res.get("content")):
            stop_ok.append((use, res))
    # ...and beta has to have actually ended: its own shutdown approval, or a
    # stop/kill notification about it reaching the lead.
    ended = [
        f"beta: {_short(text_of(b.get('content')), 120)}"
        for t in beta
        for r in t.rows
        for b in blocks(r)
        if b.get("type") == "tool_result" and "is now exiting" in text_of(b.get("content"))
    ] + [
        f"lead: {_short(json.dumps(r), 200)}"
        for r in lead.delivered()
        if "beta" in json.dumps(r)
        and re.search(
            r"<status>(killed|stopped)|was stopped|teammate_terminated|shutdown_approved",
            json.dumps(r),
        )
    ]
    # A teammate stopped with TaskStop reports nothing to the lead; what
    # shows it is gone is the team config, which drops a member that exits.
    on_peak = any("beta" in {m.get("name") for m in t.get("members", [])} for t in teams)
    if report.channel == "teams" and end_teams and on_peak:
        left = [t for t in end_teams if "beta" not in {m.get("name") for m in t.get("members", [])}]
        if left:
            members = sorted(str(m.get("name")) for m in left[0].get("members", []))
            ended.append(
                f"teams/{left[0].get('name')}/config.json no longer lists beta "
                f"(members now {members})"
            )
    report.checks.append(
        Check(
            "lead_stops_beta",
            bool(stop_ok) and bool(ended),
            f"{stop_ok[0][0].get('name')} {_short(json.dumps(stop_ok[0][0].get('input')), 120)}"
            f" -> {_short(text_of(stop_ok[0][1].get('content')), 120)}; ended: {ended[0]}"
            if stop_ok and ended
            else f"successful stops of beta={len(stop_ok)} end-evidence={ended[:1]}",
        )
    )
    done = [
        b.get("text", "")
        for r in lead.rows
        if r.get("type") == "assistant"
        for b in blocks(r)
        if b.get("type") == "text" and "PROBE DONE" in b.get("text", "")
    ]
    report.checks.append(
        Check(
            "lead_ends_turn",
            bool(done),
            f"lead final text: {_short(done[-1], 80)}" if done else "no PROBE DONE text",
        )
    )

    # The wrapper turn must stay open until the lead's work is done - else
    # the driver would think the seat finished while helpers still run.
    done_at = [
        r.get("timestamp", "")
        for r in lead.rows
        if r.get("type") == "assistant"
        and any("PROBE DONE" in b.get("text", "") for b in blocks(r) if b.get("type") == "text")
    ]
    first_done = _epoch(done_at[0]) if done_at else 0.0
    # Recorded as a wrapper finding, not a channel check: it measures the
    # wrapper's own pending-task detection (`transcript.pending_background_
    # tasks`), which is the same code whichever channel the helpers use.
    report.notes["wrapper_findings"] = {
        "turn_held_until_lead_done": bool(done_at) and report.turn_completed_at + 1.0 >= first_done,
        "evidence": f"wrapper turn completedAt={_iso(report.turn_completed_at)}, "
        f"lead PROBE DONE at={done_at[0] if done_at else 'never'}",
    }

    # Informational: how stop/completion reach the lead, and the Stop hook.
    inbound = [
        text_of((r.get("message") or {}).get("content"))
        or (r.get("attachment") or {}).get("prompt", "")
        for r in lead.delivered()
    ]
    notes = [
        _short(t, 400)
        for t in inbound
        if re.search(r"task-notification|teammate|idle|shutdown", t, re.I)
    ]
    report.notes["lead_inbound_notifications"] = notes[:16]
    log_lines = wrapper_log.splitlines()
    report.notes["stop_hook"] = {
        "lead_stop_hook_summary_rows": sum(
            1 for r in lead.rows if r.get("subtype") == "stop_hook_summary"
        ),
        "helper_stop_hook_summary_rows": {
            t.rel: sum(1 for r in t.rows if r.get("subtype") == "stop_hook_summary")
            for t in ts
            if not t.is_main
        },
        "wrapper_turn_complete_posts": sum(
            1 for line in log_lines if "POST /internal/turn-complete" in line
        ),
        "wrapper_ignored_no_turn_in_flight": sum(
            1 for line in log_lines if "no turn in flight" in line
        ),
        "wrapper_deferred_on_pending_tasks": [
            _short(line, 240) for line in log_lines if "turn stop deferred" in line
        ][:6],
    }


# --------------------------------------------------------------------------
# fixtures


def redactor(env: dict[str, str]):
    values = [
        env.get(k, "") for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "MTG_DECKS_GH_PAT")
    ]
    values = [v for v in values if len(v) >= 8]
    patterns = [
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    ]

    def redact(text: str) -> str:
        for v in values:
            text = text.replace(v, "[REDACTED]")
        for p in patterns:
            text = p.sub("[REDACTED]", text)
        return text

    return redact


def write_fixtures(channel: str, raw: Path, env: dict[str, str], report: Report) -> Path:
    target = FIXTURES / channel
    if target.exists():
        shutil.rmtree(target)
    redact = redactor(env)
    for sub in ("projects", "teams", "tasks"):
        src = raw / sub
        if not src.is_dir():
            continue
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            # projects/ keeps its layout relative to ~/.claude/projects; the
            # team/task state is kept beside it under _teams/ and _tasks/.
            rel = path.relative_to(src)
            dest = target / (rel if sub == "projects" else Path(f"_{sub}") / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(redact(path.read_text()))
            except UnicodeDecodeError:
                continue
    (target / "report.json").write_text(json.dumps(report.to_json(), indent=2) + "\n")
    return target


def stage_context(dest: Path) -> Path:
    """The images build from `..` with the engine as a sibling. In a worktree
    that sibling is a symlink, which docker never follows out of a build
    context, so both repos are mirrored into a real directory instead."""
    excludes = [".venv", "__pycache__", ".pytest_cache", "*.pyc", ".git", ".env", ".scryfall-cache"]
    flags = [f"--exclude={e}" for e in excludes]
    for repo in (REPO.parent / "magic-cards-model", REPO):
        src = repo.resolve()
        subprocess.run(
            ["rsync", "-a", "--delete", *flags, f"{src}/", str(dest / repo.name)],
            check=True,
        )
    return dest


# --------------------------------------------------------------------------


def native_platform() -> str:
    machine = platform.machine().lower()
    return "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--channel", choices=("teams", "subagents"), required=True)
    ap.add_argument("--out", type=Path, default=None, help="scratch dir for report + raw copies")
    ap.add_argument("--platform", default=native_platform())
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--timeout", type=float, default=1200.0, help="seconds for the probe turn")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--keep-up", action="store_true", help="leave the stack running")
    ap.add_argument("--no-fixtures", action="store_true")
    args = ap.parse_args(argv)

    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        print("probe: set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY (.env)", file=sys.stderr)
        return 2

    digest = hashlib.sha256(str(REPO).encode()).hexdigest()[:8]
    project = f"mcm-pair-probe-{args.channel}-{digest}"
    out = (
        args.out
        or Path(tempfile.gettempdir()) / f"probe-seat-pair-{args.channel}-{int(time.time())}"
    )
    out.mkdir(parents=True, exist_ok=True)
    empty_guides = out / "guides"
    empty_guides.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        CLAUDE_CODE_VERSION=CLAUDE_VERSION,
        DOCKER_PLATFORM=args.platform,
        MTG_DECKS=str(empty_guides),
        POSTGRES_PASSWORD=env.get("POSTGRES_PASSWORD") or "probe-unused",
        MTG_DECKS_GH_PAT=env.get("MTG_DECKS_GH_PAT", ""),
        COMPOSE_PROJECT_NAME=project,
    )
    stack = Stack(project, out, env)
    context = stage_context(Path(tempfile.gettempdir()) / f"probe-seat-pair-context-{digest}")
    stack.override.write_text(
        OVERRIDE_YAML.format(lead_model=LEAD_MODEL, effort=args.effort, context=context)
    )

    nonce = secrets.token_hex(4)
    secret = "word-" + secrets.token_hex(3)
    report = Report(channel=args.channel, nonce=nonce, secret=secret)
    try:
        if not args.skip_build:
            print(
                f"probe: building game + agent ({args.platform}, claude {CLAUDE_VERSION})",
                flush=True,
            )
            stack.compose("build", "game", "agent")
        stack.compose("up", "-d", "agent")
        wait_ready(stack, 300)
        report.claude_version = stack.sh("claude --version").strip()
        if args.channel == "teams":
            enable_teams(stack)
            snap = stack.api("GET", "/v1/session") or {}
            stack.api(
                "POST",
                "/v1/session",
                {
                    "model": snap.get("model") or LEAD_MODEL,
                    "effort": snap.get("effort") or args.effort,
                },
            )
            time.sleep(3)
            wait_ready(stack, 300)
            report.notes["home_settings"] = json.loads(
                stack.sh(f"cat {HOME}/.claude/settings.json")
            )
        uid = create_seat(stack)
        report.notes["seat_uid"] = uid
        prompt = (teams_prompt if args.channel == "teams" else subagents_prompt)(nonce, secret, uid)
        started = time.monotonic()
        record = run_turn(stack, prompt, args.timeout)
        report.turn_completed_at = float(record.get("completedAt") or 0.0)
        # The wrapper turn can end before the helpers do (see the
        # `wrapper_turn_spans_probe` check); the transcripts are read only
        # once the lead has finished or the overall budget is spent.
        peak_teams: dict[str, dict] = {}
        report.notes["settled"] = wait_settled(
            stack, args.timeout - (time.monotonic() - started), peak_teams
        )
        report.turn_state = str(record.get("state"))
        report.notes["turn_final_text"] = _short(str(record.get("finalText") or ""), 400)
        report.notes["turn_error"] = record.get("error")
        raw = out / "raw"
        transcripts = collect(stack, raw)
        log = stack.compose("logs", "--no-color", "agent", capture=True, check=False).stdout
        (out / "agent.log").write_text(redactor(env)(log))
        # The peak snapshot is what shows the members; it is kept beside
        # the end-state copy so the fixtures carry both.
        for name, team in peak_teams.items():
            peak = raw / "teams" / name / "config.peak.json"
            peak.parent.mkdir(parents=True, exist_ok=True)
            peak.write_text(json.dumps(team, indent=2) + "\n")
        end_teams = team_configs(raw)
        analyse(report, transcripts, log, list(peak_teams.values()) or end_teams, end_teams)
        if not args.no_fixtures:
            report.notes["fixtures"] = str(
                write_fixtures(args.channel, raw, env, report).relative_to(REPO)
            )
    except Exception as exc:  # noqa: BLE001 - every failure becomes a failed check
        report.checks.append(
            Check("probe_ran", False, f"{type(exc).__name__}: {_short(str(exc), 300)}")
        )
    finally:
        if not args.keep_up:
            stack.compose("down", "-v", "--remove-orphans", check=False, capture=True)
    (out / "report.json").write_text(json.dumps(report.to_json(), indent=2) + "\n")
    print(json.dumps(report.to_json(), indent=2))
    print(f"probe: report at {out / 'report.json'}")
    print(
        f"probe: {'PASS' if report.passed else 'FAIL'} "
        f"({sum(c.passed for c in report.checks)}/{len(report.checks)} checks)"
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
