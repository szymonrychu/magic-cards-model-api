# Seat pair fixtures (claude 2.1.282)

Real transcripts from `mise run probe:seat-pair -- --channel teams|subagents`
(`scripts/probe_seat_pair.py`): the agent image built from `Dockerfile.agent`
with `CLAUDE_CODE_VERSION=2.1.282`, the wrapper's PTY claude (Sonnet 5 lead,
`bypassPermissions`, the Stop hook, no tmux) driving two Opus helpers `alpha`
and `beta` through one wrapper turn. Keys are redacted. Each channel directory
holds the probe's own `report.json` (per check: pass/fail and one evidence
line) for the run its transcripts come from.

## File layout

Paths under each channel directory are relative to `~/.claude/projects`
(`/agent-home/.claude/projects` in the container), so a test can point a
fake HOME at a copy and read them where the wrapper would.

```
<channel>/
  -workspace/                              project dir = the cwd /workspace, encoded
    <sessionId>.jsonl                      the lead (main session) transcript
    <sessionId>/subagents/
      agent-<agentId>.jsonl                one per helper (sidechain transcript)
      agent-<agentId>.meta.json            the helper's spawn metadata
  _teams/session-<sessionId[:8]>/          teams only: ~/.claude/teams/<team>/
    config.json                            end state (stopped members are removed)
    config.peak.json                       the probe's fullest snapshot while it ran
    inboxes/<name>.json                    per-member mailbox (empty once read)
  report.json
```

`_teams/` is `~/.claude/teams/` (not under `projects/`); `~/.claude/tasks/
session-<id8>/` exists in both channels but stays empty, so nothing of it is
kept.

## Fields that identify an agent

| Where | Field | Lead | Teams helper | Subagents helper |
|---|---|---|---|---|
| every transcript row | `isSidechain` | `false` | `true` | `true` |
| every transcript row | `sessionId` | own session id | the LEAD's session id | the LEAD's session id |
| every helper row | `agentId` | absent | `a<name>-<16 hex>`, e.g. `aalpha-8970442a8800e065` | `a<17 hex>`, e.g. `a7ae15c896fde5555` |
| assistant rows | `message.model` | `claude-sonnet-5` | `claude-opus-5-5` | `claude-opus-5-5` |
| `agent-<id>.meta.json` | `name` | - | `alpha` | `alpha` |
| `agent-<id>.meta.json` | `teamName` | - | `session-<id8>` | absent |
| `agent-<id>.meta.json` | `taskKind` | - | `in_process_teammate` | absent |
| `agent-<id>.meta.json` | `model` | - | `opus` (the alias asked for) | `opus` |
| `agent-<id>.meta.json` | `toolUseId`, `spawnDepth` | - | absent, `0` | the lead's Agent `tool_use` id, `1` |
| team `config.json` member | `agentId`, `name`, `backendType` | `team-lead@session-<id8>`, `team-lead`, `in-process` | `alpha@session-<id8>`, `alpha`, `in-process` | - |

The helper's name is NOT on its transcript rows; it is only in the
`.meta.json` (and, for teams, the team config). The transcript `agentId`
(`aalpha-...`) and the team config `agentId` (`alpha@session-...`) are
different spellings of the same teammate. The lead's Agent `tool_result`
carries `toolUseResult`:

- teams: `{"status": "teammate_spawned", "agent_id": "alpha@session-<id8>",
  "name": "alpha", "team_name": "session-<id8>", "model": "opus",
  "tmux_pane_id": "in-process", ...}`
- subagents: `{"isAsync": true, "status": "async_launched", "agentId":
  "a<17 hex>", "resolvedModel": "claude-opus-5-5", "outputFile": ...}`

## Teams in 2.1.282

There is no `TeamCreate`/`TeamDelete` tool (asked for by name, the model gets
"No TeamCreate tool is available"). With `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
and `teammateMode: "in-process"` the session has a single implicit team
`session-<id8>` whose lead is `team-lead`; Agent's `team_name` is "Deprecated;
ignored", and every Agent call with a `name` spawns an in-process teammate.

## How messages, stops and completion reach the lead

Teams (all arrive as `user` rows whose string content starts with
`Another Claude session sent a message:`, several may be batched in one row):

- a teammate message: `<teammate-message teammate_id="beta" color="green"
  summary="..."> BETA GOT NONCE ... </teammate-message>`
- a teammate going idle after each of its turns: `<teammate-message
  teammate_id="alpha"> {"type":"idle_notification","from":"alpha",
  "timestamp":...,"idleReason":"available","summary":"[to beta] ...",
  "result":"<its last text>"} </teammate-message>`
- shutdown by `SendMessage {"type":"shutdown_request"}`: the teammate gets
  the request with a `requestId`, answers `shutdown_response` `approve: true`
  ("Agent beta is now exiting"), and the lead gets `<teammate-message
  teammate_id="system"> {"type":"teammate_terminated","message":"beta has
  shut down."} </teammate-message>`
- stop by `TaskStop {"task_id": "beta"}`: the result is `{"message":
  "Successfully stopped task: t<id> (...)","task_type":"in_process_teammate"}`
  and NO notification follows; the member disappears from `config.json`.
- teammate-to-teammate `SendMessage` lands in the recipient's transcript as
  `<teammate-message teammate_id="alpha" ...>`; the lead sees only the
  sender's `idle_notification` summary (`"[to beta] ..."`).

Subagents (background Agent with a `name`):

- completion, each time the agent stops: `<task-notification><task-id>
  a<17 hex></task-id><tool-use-id>toolu_...</tool-use-id><output-file>/tmp/
  claude-1000/-workspace/<sessionId>/tasks/<id>.output</output-file><status>
  completed</status><summary>Agent "<description>" finished</summary><note>
  ...</note><result>TEXT</result><usage>...</usage></task-notification>` - as a
  `user` row when the lead is idle, or as an `attachment` of type
  `queued_command` (`commandMode: "task-notification"`) when it lands
  mid-turn, alongside `queue-operation` enqueue/remove rows.
- continuation: `SendMessage {"to": "alpha", ...}` to a finished agent answers
  `{"success":true,"message":"Resuming agent alpha","resumedAgentId":...}`;
  the agent continues in the SAME transcript file (same `agentId`) and the
  same task id notifies again.
- stop: `TaskStop {"task_id": "beta"}` answers `{"message":"Successfully
  stopped task: <id> (<description>)","task_type":"local_agent"}`, followed by
  a task-notification with `<status>killed</status><summary>Agent "..." was
  stopped by Claude</summary>` and no `<result>`.
- there is no peer messaging: the lead relays alpha's result to beta itself.

## The Stop hook

The Stop hook fires for the lead only: helper transcripts carry no
`stop_hook_summary` rows, and the wrapper's `POST /internal/turn-complete`
count equals the lead's `stop_hook_summary` count. It fires every time the
lead goes idle, and every teammate message or task-notification wakes the
lead into a new conversation turn, so one wrapper turn sees several Stop
hooks; after the first that completes the turn the wrapper logs
`turn-complete with no turn in flight, ignoring` for the rest.

Whether the wrapper turn stays open until the helpers are done is recorded
per run in `report.json` under `notes.wrapper_findings`:

- teams: never held. `transcript.pending_background_tasks` does not recognise
  the `teammate_spawned` Agent result, so the first lead idle (right after the
  spawns) completes the wrapper turn while both teammates still run.
- subagents: partly held. The Stop hook lists the two `async_launched`
  agents as pending (`turn stop deferred ... 2 background task(s) still
  running`) until each first reports back, but an agent resumed by
  `SendMessage` ("Resuming agent ...") is not tracked as a launch, so the
  turn completed at the next lead idle, 11s before `PROBE DONE` in the
  committed run. One earlier run did not defer at all (the hook read the
  transcript before the launch results were on disk, `stop_reason=tool_use`).
