# MTGO-like play UI, action protocol, and per-game agent pods

Date: 2026-09-04
Topic: (a) how mature MTG clients present casting, ability activation, mana
payment, per-mode targeting, priority stops and combat, and what the
client-server protocol looks like in rules-enforcing open-source engines;
(b) scheduling one Kubernetes pod per game on demand from a FastAPI service,
with per-game model and effort settings for the Claude Code CLI.

Four researchers (prior art, MTGO/Arena UX, Kubernetes docs, Kubernetes
practice) plus an adversarial reviewer. Claims the reviewer contradicted are
dropped or corrected below. Codebase findings come from three read-only
explorations of `magic-cards-model-api` and `magic-cards-model`.

## Executive summary

- Rules-enforcing clients converge on one protocol shape: the server owns
  legality and asks the client typed questions (choose modes, choose targets
  for this mode, pay this cost, confirm attackers), one prompt per decision
  type [1][2][4]. XMage makes each mode own its own target list [3]; Forge
  chooses all modes first, then targets per chosen mode [2].
- The interaction the user expects is click-to-cast, then click legal targets
  highlighted on the board; a permanent with one ability activates on click,
  one with several opens a short text menu [5][6]. Mana is auto-paid with a
  manual override that is per decision, not one global toggle [7][8].
- Priority automation is tiered (yield once, this turn while the stack is
  empty, this turn regardless, whenever nothing is castable). The tiers are
  stable across MTGO, Arena and XMage; the key bindings are not, and the
  current MTGO client binds them to number keys [5]. Design on the tiers.
- The engine already supports per-mode targets end to end; the API already
  tags each candidate with the requirement slots it can fill; only the
  browser flattens targets into a set (see "Codebase").
- For per-game pods: use a Job (not a bare Pod) with `activeDeadlineSeconds`,
  `ttlSecondsAfterFinished`, `restartPolicy: Never`, explicit
  `ownerReferences` set by our service, namespace-scoped RBAC under the
  restricted Pod Security Standard, list-watch plus a periodic label-selector
  reconcile as the backstop every surveyed system ended up needing [9-16].
- Claude Code exposes model and effort as `--model`/`--effort` flags and
  `ANTHROPIC_MODEL`/`CLAUDE_CODE_EFFORT_LEVEL` env vars; `--effort` accepts
  `low|medium|high|xhigh|max` (model dependent) [17][18].

## 1. Prior art: protocol shape in rules-enforcing engines

- Forge's `PlayerController` is the decision interface between engine and any
  controller: `chooseTargetsFor`, `payManaCost`, `chooseModeForAbility(sa,
  min, num)`, `declareAttackers`, `declareBlockers`, `chooseNumber` [1]. The
  human implementation is a blocking Input state machine, one `Input*` class
  per decision type with `showAndWait()`; modes are chosen to completion
  before any targeting starts [2] (reviewer: confirmed; the mana class is
  `InputPayManaOfCostPayment`).
- XMage's `Modes.choose()` loops until `selectedModes >= maxModes` and each
  `Mode` owns `getTargets()`, so per-mode targets are structural, not a UI
  convention. `MAX_MODES_TO_SELECT = 5` [3] (reviewer: confirmed in source).
- XMage's client shows a precomputed "playable abilities" count and highlights
  valid targets while targeting [4] (release notes, medium confidence).
- XMage's pass flags (`passedTurn`, `passedUntilEndOfTurn`,
  `passedUntilNextMain`, `passedUntilStackResolved`) mirror MTGO's yield
  tiers conceptually [4]. The specific F-key mapping claimed by two
  researchers is contradicted by the live MTGO page, which binds them to
  number keys 1, 2, 6, 8 with 5 clearing yields [5].
- Cockatrice and Untap.in are the negative case: a protobuf Command/Event
  relay with no rules engine at all [19][20].
- No mature TypeScript/JavaScript rules engine with a browser UI was found
  (medium confidence, search only).

## 2. Player-facing conventions (MTGO, Arena)

- Casting: castable cards are highlighted; clicking one moves it toward the
  stack; targets are chosen by clicking the permanent or player avatar under
  an on-screen prompt. A card with several options (modes, alternative or
  additional costs, multi-colour mana) opens a menu with one line per option
  [6]. Holding Ctrl through the cast holds priority [5].
- Abilities: a permanent with one ability activates on click; several open
  the one-line-per-option menu; right-click carries yield options [5][6].
  Arena's ability menu could not be verified as list or radial (gap).
- Mana: Arena auto-taps by default; holding Ctrl (Full Control) taps manually
  and dragging the spell forward previews what will be tapped [7]. The
  auto-tapper's failures (stranding the second spell, avoiding painlands,
  mishandling X and cost reduction) are the community's standing complaint
  [8][21]; whether Full Control now overrides mana spend once tapped is not
  confirmed for 2026 (2019 thread) [22]. MTGO shows the floating pool
  persistently and lets Ctrl+Z undo a mana ability before it feeds the stack
  [5].
- Priority tiers (concept, not bindings): yield once; auto-yield this turn
  while the stack is empty; auto-yield this turn even with a stack (still
  stops for yes/no and combat); auto-yield whenever no legal action; clear
  all [5][23]. MTGO's "no legal action" tier evaluates printed costs and
  misses cost reduction (confirmed bug) [24]; Arena's Full Control is hold
  Ctrl for one phase, Ctrl+Shift persistent; Space passes, Enter passes until
  a response is needed, Shift+Enter passes the turn [7][25].
- Combat: MTGO clicks a creature into the red zone, menus a defender when
  several are legal, right-click attacks with all; blocking is click own
  creature then the attacker; damage order is click order, lethal first [6].
- Decisions: scry/surveil present each card with two drop targets [26].
- Cryptic-style modal spells: modes must differ (CR 700.2d); whether targets
  may repeat across modes is a rules question the researchers sourced weakly.
  Verified in the engine repo instead: CR 115.3 forbids the same target only
  for one instance of the word "target", so two modes may share a target.

## 3. Kubernetes: one pod per game

- Kubernetes docs: "Pods alone are not resilient to machine failures, but
  controllers are"; create a controller and let it create Pods [9]. Job is
  the documented run-to-completion controller: `activeDeadlineSeconds`
  (relative to startTime, ends in `Failed/DeadlineExceeded`, takes precedence
  over `backoffLimit`), `backoffLimit` default 6, `ttlSecondsAfterFinished`
  cleans Complete and Failed Jobs, template `restartPolicy` must be `Never`
  or `OnFailure` [10] (reviewer: all confirmed).
- `PodSpec.activeDeadlineSeconds` also exists on bare Pods [11].
- `ownerReferences` require name plus UID in the same namespace. Kubernetes
  sets them automatically for native controllers; a custom service creating
  Jobs directly must set them itself if it wants garbage collection of its
  children [12] (reviewer: caveat restored).
- RBAC: a namespaced Role over `pods` and `batch/jobs` with
  `create/get/list/watch/delete`, bound to the API's ServiceAccount;
  `automountServiceAccountToken: false` on pods that do not need the API,
  pod spec wins over ServiceAccount when both set [13][14].
- Restricted Pod Security Standard: `privileged: false`, drop ALL
  capabilities, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
  seccomp `RuntimeDefault` [15]. Granting create-pods is a documented
  privilege-escalation vector unless namespace-scoped and paired with the
  restricted profile [16][27][28].
- Python client: `kubernetes` 36.x with `config.load_incluster_config()`,
  `BatchV1Api.create_namespaced_job`, `watch.Watch().stream(...)`,
  `V1DeleteOptions(propagation_policy="Background")`; `kubernetes_asyncio`
  36.x mirrors it async. FastAPI runs plain `def` routes in a threadpool, so
  the sync client is usable from routes that are not `async def` [29][30][31].
- Practice: every surveyed system (KubeSpawner, actions-runner-controller,
  JupyterHub) that tracks children through a watch alone accumulated
  zombie or stuck sessions; the fix each adopted is a periodic full reconcile
  by owner label on top of the watch [32][33][34][35].
- Child-calls-parent over the parent's Service DNS with a per-session token in
  env (JupyterHub's model) is more robust than the parent reaching pod IPs
  [36]. Pod networking is flat unless a NetworkPolicy restricts it [37].
- Startup latency: DaemonSet image pre-pull is the cheap lever; warm pools
  (Agones `bufferSize`) only when idle pods are cheap [38][39]. kubelet's
  default `registryPullQPS` of 5/s throttles parallel pulls [40].
- Limits: `ResourceQuota` (pod count, CPU, memory, scoped by Terminating and
  PriorityClass) plus `activeDeadlineSeconds` on every session Job [41][42].

## 4. Claude Code model and effort configuration

- `--model` accepts an alias (`sonnet`, `opus`, `haiku`, `fable`) or a full
  name and overrides the `model` setting and `ANTHROPIC_MODEL` [17].
- `--effort low|medium|high|xhigh|max|ultracode` overrides `modelSettings`
  and `effortLevel` for the session and does not persist [17].
- Env: `ANTHROPIC_MODEL`; `CLAUDE_CODE_EFFORT_LEVEL` (a level or `auto`);
  `MAX_THINKING_TOKENS` (0 disables thinking except on Fable 5.1 and 5) [18].
- Settings keys: `model`, `effortLevel`, `modelSettings` (per model),
  `alwaysThinkingEnabled` [18][43]. The reviewer corrected one researcher:
  `max` is valid everywhere effort is supported; it is `xhigh` that Opus 4.6
  and Sonnet 4.6 lack. Default is `high` except Opus 4.7 (`xhigh`) [18].

## Best practices

1. Server owns legality; the client renders typed prompts and never derives
   a rule from card text.
2. Modes first, then targets scoped per mode, with each candidate tagged by
   the slot it fills. Allow the same object in two different slots.
3. One click for a single-ability permanent or a mana ability; a short menu
   only when there is a real choice; no confirm step when nothing is left to
   choose.
4. Auto-pay mana with a preview of what will be tapped and a per-action
   manual override; keep the floating pool visible.
5. Tiered yield, exposed as verbs (pass, pass this turn, pass until my next
   turn, pass unless something happens) rather than bound keys.
6. Session pods as Jobs with deadline, TTL, explicit ownerReferences,
   restricted PSS, namespaced RBAC, list-watch plus reconcile sweep, and a
   per-game token in env so the child calls the parent.

## Prior art to borrow

- XMage `Modes`/`Target` for the mode-owns-targets model [3].
- Forge `Input*` per decision type for how the client structures prompt
  handling [2].
- JupyterHub KubeSpawner for per-session pods with a callback token [36], and
  its orphan-cleanup issue for what to build up front [34].

## Open questions and gaps (from the reviewer)

- Key bindings differ between sources and MTGO client versions. Do not
  encode any; expose the tiers as buttons and let bindings be a later
  setting.
- Arena's ability menu shape and current Full Control mana behaviour are
  unverified for 2026.
- Headless Claude Code inside a fresh pod (auth, permission mode) was not
  researched externally; the existing wrapper in this repo already solves it
  with `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` and a settings.json
  bypass, so it is a codebase fact, not a research gap.
- A NetworkPolicy pattern restricting an agent pod's egress to the game API
  and the MCP endpoints was not sourced; it is standard Kubernetes and can be
  written directly.
- The "shared long-lived LLM keys are the common incident" claim is single
  sourced.

## Codebase facts that shape the design (verified in-tree)

- The engine parses each chosen mode separately, assigns a flat target list
  back to per-mode slots (`targeting.py:1551`, `casting.py:2859`), carries
  `mode_targets` onto the stack and resolves each mode against its own
  slice. CR 115.3 is enforced per requirement instance, so the same player
  can fill mode 1's and mode 2's "target player".
- `GET /affordances/{card}` returns one flat `candidates` list where each
  candidate carries `requirements: [slot indices]`; the slot-to-mode owner
  is computed by the engine but discarded by the API layer.
- `ui/app.js` stores targets as a set (`toggleTarget` removes on second
  click), never reads `candidate.requirements` or `division`, never sends
  `mana_sources`, never uses `dry_run`, tap-for-mana and every other action
  end in a confirm button, multi-ability permanents go through a blocking
  modal and then the confirm, attackers and blockers are modals, there is no
  game log, and the board is rebuilt from `innerHTML` every 1.2 s.
- `seatkit.py` is the shared layer for MCP and CLI; the browser is a third
  implementation of the same questions in JavaScript.
- The store already serves several games; the single-game assumptions live in
  the driver (one global assignment, 409 on a second game), the MCP server's
  "pick games[0]" default, `lobby.reset_previous_games`, and the UI's single
  localStorage slot.
- The agent runs interactive `claude` under a PTY inside one three-container
  Deployment; the only model setting is `MODEL=sonnet` becoming `--model`;
  there is no effort or thinking setting anywhere; no RBAC exists.

## Sources

1. https://github.com/svn2github/forge/blob/master/forge-game/src/main/java/forge/game/player/PlayerController.java
2. https://raw.githubusercontent.com/Card-Forge/forge/master/forge-gui/src/main/java/forge/player/PlayerControllerHuman.java
3. https://raw.githubusercontent.com/magefree/mage/master/Mage/src/main/java/mage/abilities/Modes.java
4. https://github.com/magefree/mage/blob/master/Mage/src/main/java/mage/players/PlayerImpl.java
5. https://www.mtgo.com/getting-started/getting-started-tips-tricks
6. https://www.mtgo.com/getting-started/getting-started-gameplay
7. https://mtgazone.com/arena-hot-keys-and-interface-guide-simplify-your-game-with-these-easy-tricks/
8. https://steamcommunity.com/app/2141910/discussions/0/686367559407216959/
9. https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
10. https://kubernetes.io/docs/concepts/workloads/controllers/job/
11. https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#PodSpec
12. https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/
13. https://kubernetes.io/docs/reference/access-authn-authz/rbac/
14. https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/
15. https://kubernetes.io/docs/concepts/security/pod-security-standards/
16. https://github.com/kubernetes/website/pull/5551
17. https://code.claude.com/docs/en/cli-reference
18. https://code.claude.com/docs/en/model-config
19. https://github.com/Cockatrice/Cockatrice/wiki/Client-server-protocol
20. https://untap.in/
21. https://commandersherald.com/heartwarming-arena-auto-tapper-leaves-up-correct-mana-for-the-spell-she-wanted-to-cast-next/
22. https://devtrackers.gg/magic-arena/p/c8ab7de2-they-need-to-let-you-have-full-control-of-floating-mana-again
23. https://www.mtgsalvation.com/forums/the-game/other-formats/488802-how-do-i-not-lose-to-time
24. https://forums.mtgo.com/index.php?threads/mystical-dispute-cost-reduction-spells-and-f8-auto-yield.51/
25. https://sites.google.com/view/beginners-guide-to-mtga/hotkeys-and-using-the-interface
26. https://draftsim.com/mtg-surveil/
27. https://www.aquasec.com/blog/privilege-escalation-kubernetes-rbac/
28. https://unit42.paloaltonetworks.com/kubernetes-privilege-escalation/
29. https://pypi.org/project/kubernetes/
30. https://pypi.org/project/kubernetes-asyncio/
31. https://fastapi.tiangolo.com/async/
32. https://github.com/jupyterhub/kubespawner/issues/577
33. https://github.com/actions/actions-runner-controller/issues/4197
34. https://github.com/jupyterhub/jupyterhub/issues/4544
35. https://oneuptime.com/blog/post/2026-09-03-kubernetes-watch-410-gone-relist-reconcile/view
36. https://jupyterhub.readthedocs.io/en/latest/reference/api/services.auth.html
37. https://www.systemshardening.com/articles/kubernetes/containerssh-kubernetes-backend/
38. https://oneuptime.com/blog/post/2026-02-08-how-to-use-docker-image-pre-pulling-strategies/view
39. https://agones.dev/site/docs/reference/fleetautoscaler/
40. https://www.netdata.cloud/academy/gitlab-runner-executor/
41. https://kubernetes.io/docs/concepts/policy/resource-quotas/
42. https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/
43. https://code.claude.com/docs/en/settings-reference
