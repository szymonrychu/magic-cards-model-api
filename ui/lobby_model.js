// Pure lobby logic: no DOM, no `fetch` - `ui/render/lobby.js` is the module
// that actually talks to the network and the page; this is what it hands its
// results through and what `tests/test_ui_lobby.py` runs straight under Node.
//
// `lobbyViewModel` joins `GET /api/lobby/games`'s server rows with the browser's
// own saved seats (`mcm.games.v1`, see the storage helpers below) into one
// row per game: the opponent's name, where the game currently sits, its AI
// opponent's model/effort, and the two uids `handlers.startGame`/`DELETE`
// need. `agentFrom` turns the AI deck picker's three raw fields (`#ai-model`,
// `#ai-effort`, `#ai-thinking`) into the `agent` body `POST /api/lobby/games`
// sends for the AI player, the same shape `AgentSettings` (the API's pydantic
// model) accepts.

export const MODEL_SUGGESTIONS = ["sonnet", "opus", "haiku", "fable"];
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

/** Whether a trimmed `#ai-thinking` value is acceptable: empty (meaning "let
 * the agent decide" - `agentFrom` sends `null` for that) or a whole,
 * non-negative number. `AgentSettings.max_thinking_tokens` (the API's
 * pydantic field) is `int | None = Field(ge=0)` - a fraction ("5.5") or a
 * negative number is not a legal value for it, so both must be rejected
 * here rather than silently become `null` and start the game anyway with
 * the model's default budget. `Number.parseInt` alone is not enough for
 * this: `Number.parseInt("5.5", 10)` is `5`, truncating instead of
 * rejecting - `Number()` plus `Number.isInteger` catches the fraction
 * `parseInt` would silently swallow. */
export function thinkingTokensAreValid(raw) {
  const trimmed = String(raw === undefined || raw === null ? "" : raw).trim();
  if (trimmed === "") return true;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) && parsed >= 0;
}

export function agentFrom(fields) {
  const model = (fields.model || "").trim() || "sonnet";
  const effort = EFFORTS.includes(fields.effort) ? fields.effort : "medium";
  const thinking = String(fields.thinking === undefined || fields.thinking === null ? "" : fields.thinking).trim();
  const max_thinking_tokens = thinkingTokensAreValid(thinking) && thinking !== "" ? Number(thinking) : null;
  return { model, effort, max_thinking_tokens };
}

/** A saved game the server's list no longer carries - one this browser
 * ended, which `DELETE /api/lobby/games/{id}` soft-deletes (`archived_at`)
 * rather than destroys, so its ledger is still there to review. Everything
 * shown for it comes from the saved entry itself, because that is all there
 * is: `list_lobby_games` filters archived games out, and asking the server
 * about one would be a second round trip to be told the same thing. */
function archivedRow(entry) {
  return {
    gameId: entry.gameId,
    opponent: entry.opponent || null,
    turn: entry.turn === undefined ? null : entry.turn,
    phase: null,
    over: true,
    archived: true,
    endedAt: entry.endedAt || null,
    model: entry.model || null,
    effort: entry.effort || null,
    myUid: entry.uid || null,
    aiUid: entry.aiUid || null,
  };
}

export function lobbyViewModel(games, saved) {
  const mine = new Map((saved || []).map((s) => [s.gameId, s]));
  const listed = new Set((games || []).map((g) => g.game_id));
  const ended = (saved || [])
    .filter((s) => s.endedAt && !listed.has(s.gameId))
    .map((s) => archivedRow(s));
  return [
    ...(games || [])
    // `list_lobby_games` lists every lobby-booted game on the server, not
    // only this browser's own - its own `seats` filter (only uids in the
    // caller's `?uids=` query) is what actually narrows it, so a game this
    // browser neither saved nor holds a seat uid in must never render: an
    // empty `seats` with no saved entry means neither is true, and the row
    // would otherwise show this browser's own name as some other browser's
    // "opponent".
    .filter((g) => mine.has(g.game_id) || Object.keys(g.seats || {}).length > 0)
    .map((g) => {
      const seat = mine.get(g.game_id) || {};
      // `seats` maps username -> uid. Prefer the username whose uid equals
      // this saved entry's own `uid` (the seat this browser actually
      // started or opened this game as) over "whichever key comes first" -
      // object key order here is the server's own dict iteration order, and
      // `refreshGames` sends *both* this browser's own uid and its aiUid in
      // the `?uids=` query, so a server response can legally list the AI
      // seat's username ahead of the human one in `seats`. Falling back to
      // the first key only when there is no saved uid to match against
      // (a game this browser holds a seat in by uid alone, never saved).
      const me =
        (seat.uid && Object.keys(g.seats || {}).find((name) => g.seats[name] === seat.uid)) ||
        Object.keys(g.seats || {})[0] ||
        null;
      const opponent = (g.players || []).find((p) => p !== me) || (g.players || [])[1] || null;
      const agent = opponent && g.agents ? g.agents[opponent] : null;
      return {
        gameId: g.game_id,
        opponent,
        turn: g.turn_number,
        phase: g.phase,
        over: Boolean(g.is_over),
        archived: false,
        endedAt: null,
        model: agent ? agent.model : null,
        effort: agent ? agent.effort : null,
        myUid: seat.uid || (me ? g.seats[me] : null),
        aiUid: seat.aiUid || null,
      };
    }),
    ...ended,
  ];
}

// ------------------------------------------------------------- storage ----
//
// `mcm.games.v1` is the list of games this browser has ever started or
// opened, `[{gameId, uid, aiUid}]` - the lobby's own record of "seats I
// hold", since the server itself never hands out a uid to anyone who doesn't
// already have it (`list_lobby_games`'s own `uids` filter). It replaces the
// old single-game `mcm.game` key (`{gameId, uid, aiUid}`, no list) from
// before the lobby could track more than one game at a time; `loadSavedGames`
// migrates that old key into the new list, once, the first time anything
// reads it.
//
// Both `ui/app.js` (appending on `startGame`) and `ui/render/lobby.js`
// (reading the list for its `GET /api/lobby/games?uids=...` query, and dropping
// a row on End) go through these rather than each rolling its own
// `JSON.parse`/`JSON.stringify`, so the two never drift on the key name or
// the migration.

export const SAVED_GAMES_KEY = "mcm.games.v1";
export const OLD_SAVED_GAME_KEY = "mcm.game";

/** The saved-games list, migrating the old single-game key into it the first
 * time this is called. `storage` is `localStorage` in the browser, or `null`
 * where none exists (a Node test with no `globalThis.localStorage` stubbed);
 * either an absent, corrupted, or unreadable key answers `[]` rather than
 * throwing. */
export function loadSavedGames(storage) {
  if (!storage) return [];
  try {
    const raw = JSON.parse(storage.getItem(SAVED_GAMES_KEY) || "null");
    if (Array.isArray(raw)) return raw;
  } catch {
    // fall through to the migration below
  }
  try {
    const old = JSON.parse(storage.getItem(OLD_SAVED_GAME_KEY) || "null");
    if (old && old.gameId) {
      const migrated = [{ gameId: old.gameId, uid: old.uid, aiUid: old.aiUid }];
      saveGames(storage, migrated);
      storage.removeItem(OLD_SAVED_GAME_KEY);
      return migrated;
    }
  } catch {
    // a corrupted old key is no worse than none - nothing to migrate
  }
  return [];
}

export function saveGames(storage, list) {
  if (!storage) return;
  storage.setItem(SAVED_GAMES_KEY, JSON.stringify(list));
}

/** Add or replace `entry` by `gameId` - `startGame` calls this both for a
 * freshly created game and for reopening one already in the list (Open on an
 * existing row), so it must not duplicate the row either time. */
export function upsertSavedGame(list, entry) {
  return [...(list || []).filter((g) => g.gameId !== entry.gameId), entry];
}

export function dropSavedGame(list, gameId) {
  return (list || []).filter((g) => g.gameId !== gameId);
}
