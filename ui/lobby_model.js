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
    unloadable: null,
    // Only a listed row can carry one (`decks_api.LobbyGameSummary.
    // recorded_ending`), and an ended game is not listed - `archived` is
    // already the reason its review may be opened.
    recordedEnding: null,
    endedAt: entry.endedAt || null,
    model: entry.model || null,
    effort: entry.effort || null,
    myUid: entry.uid || null,
    aiUid: entry.aiUid || null,
  };
}

/** Which seat of `game` (one server `LobbyGameSummary`) is this player's, and
 * which is the AI's - `{me, myUid, aiUid}`, every field `null` for a row that
 * carries no seats at all.
 *
 * Three sources, in order, and the order is the whole point:
 *
 * 1. `savedUid` - this browser's own entry in `mcm.games.v1`. If it names a
 *    seat of this game, that is the seat, full stop: it is the one this
 *    browser actually started or opened the game as, and it wins even when
 *    it happens to be the AI's seat (a browser that started the game holds
 *    both uids).
 * 2. The seat with no entry in `game.agents`. This is what makes a *second
 *    computer* work: it has nothing saved, and every uid in the row came
 *    from the server because the logged-in user owns the game
 *    (`decks_api.list_lobby_games` widens `seats` for the owner alone). The
 *    row already says which seats are AI-driven, so the one that is not is
 *    the human's.
 * 3. Whichever key comes first. Reached only for a human-vs-human game,
 *    where (2) matches both seats and there is nothing left to distinguish
 *    them by - so pick one and stay usable rather than throw or answer
 *    `null`. Such a game has no AI seat, so `aiUid` is correctly `null`.
 *
 * Key order is deliberately never a *signal* here: it is the server's own
 * dict iteration order, and the AI seat can legally be listed first. */
export function seatsFromRow(game, savedUid) {
  const seats = (game && game.seats) || {};
  const agents = (game && game.agents) || {};
  const names = Object.keys(seats);
  if (!names.length) return { me: null, myUid: null, aiUid: null };
  const matched = savedUid ? names.find((name) => seats[name] === savedUid) : null;
  const me = matched || names.find((name) => !agents[name]) || names[0];
  const aiName = names.find((name) => name !== me && agents[name]) || null;
  return { me, myUid: seats[me], aiUid: aiName ? seats[aiName] : null };
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
    // only this browser's own - a non-empty `seats` is what says "I hold a
    // capability for this one", and it is now true in two ways rather than
    // one: the uids this browser presented in `?uids=` (as before), OR every
    // seat of a game the *logged-in user owns*, which the server hands the
    // owner unasked. That second way is what lets a second computer, with an
    // empty `mcm.games.v1`, see and open a game it has never touched.
    //
    // A row that is neither saved nor seated must still never render: it
    // would show this browser's own name as some other browser's "opponent".
    .filter((g) => mine.has(g.game_id) || Object.keys(g.seats || {}).length > 0)
    .map((g) => {
      const seat = mine.get(g.game_id) || {};
      // Which of the two seats is this player's - saved uid first, then the
      // seat with no agent, then key order. See `seatsFromRow`.
      const { me, myUid, aiUid } = seatsFromRow(g, seat.uid);
      const opponent = (g.players || []).find((p) => p !== me) || (g.players || [])[1] || null;
      const agent = opponent && g.agents ? g.agents[opponent] : null;
      return {
        gameId: g.game_id,
        opponent,
        turn: g.turn_number,
        phase: g.phase,
        over: Boolean(g.is_over),
        archived: false,
        // Why this game cannot be opened, in the engine's own words, or
        // null. The server lists such a game deliberately rather than
        // dropping it (`decks_api.LobbyGameSummary.unloadable`) - a game
        // somebody played must not appear to have vanished - so the row is
        // built exactly like any other, from the same seats/agents fields,
        // and it is the renderer that withholds Open.
        unloadable: g.unloadable || null,
        // The `seq` of this game's `game_over` row, for an undecodable row
        // only - the server computes it for no other kind (see
        // `decks_api.LobbyGameSummary.recorded_ending`). It is the only
        // thing that can establish that such a game finished, and so the
        // only thing that says its review will actually read rather than
        // 409.
        recordedEnding: g.recorded_ending === undefined ? null : g.recorded_ending,
        endedAt: null,
        model: agent ? agent.model : null,
        effort: agent ? agent.effort : null,
        // The saved entry still wins where there is one; the server-supplied
        // uid is what a browser with nothing saved falls back on, and is the
        // only thing that makes such a row openable at all.
        myUid: seat.uid || myUid || null,
        aiUid: seat.aiUid || aiUid || null,
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
