// Pure lobby logic: no DOM, no `fetch` - `ui/render/lobby.js` is the module
// that actually talks to the network and the page; this is what it hands its
// results through and what `tests/test_ui_lobby.py` runs straight under Node.
//
// `lobbyViewModel` joins `GET /api/lobby/games`'s server rows with the browser's
// own saved seats (`mcm.games.v1`, see the storage helpers below) into one
// row per game: the opponent's name, where the game currently sits, its AI
// opponent's model/effort (server-reported, read-only - see `archivedRow`/
// `lobbyViewModel` below), and the two uids `handlers.startGame`/`DELETE`
// need. `seatEntry` builds each `players[]` entry of the create-game
// request, including the human seat's opt-in buddy (`buddy`), and
// `buddyControlState` says whether a seat's buddy toggle may be used.
//
// 2026-09-19 (owner direction, cost cutting): model, effort and thinking
// budget are no longer a per-game choice. `NewUiPlayer` (the API's request
// model) carries no field for any of them any more - the server always runs
// `AgentSettings.default()`, fixed to Sonnet at high effort, for an AI seat
// and a buddy alike. The picker this module used to build request bodies
// from (`agentFrom`, `MODEL_SUGGESTIONS`, `EFFORTS`, `DEFAULT_MODEL`,
// `thinkingTokensAreValid`) is gone with it; `seatEntry` sends `agent`/
// `buddy_agent` for nothing, ever.

/** One `players[]` entry of `POST /api/lobby/games`, decided by whether the
 * seat is an AI seat - never by which controls happen to exist on the page.
 * Carries no `agent`/`buddy_agent` (2026-09-19: model is a server-side
 * choice, not a request field). An AI seat carries nothing beyond `ai`
 * itself: the server refuses a buddy on one (`decks_api.NewUiPlayer.buddy`),
 * since it plays through its own uid and never reads advice, so a `buddy`
 * asked for on an AI seat is dropped here rather than sent to be refused. A
 * human seat carries `buddy: true` only when opted in. */
export function seatEntry({ username, decklistText, ai, buddy }) {
  const entry = { username, decklist_text: decklistText, ai: Boolean(ai) };
  if (!entry.ai && buddy) {
    entry.buddy = true;
  }
  return entry;
}

/** Whether a seat's buddy toggle is usable: never on an AI seat, whatever
 * the toggle says; always on a human seat. */
export function buddyControlState({ ai }) {
  return { toggleDisabled: Boolean(ai) };
}

/** The first 8 hex chars of a `game_id` (a bare `uuid4().hex`, `app.py`'s
 * own docstring) - the same prefix a human already sees in server logs and
 * a game's own URL, so it is a stable, at-a-glance way to tell two rows
 * apart that otherwise look identical (same opponent, same model/effort,
 * even the same rendered "when"). `"-"` for a row with no id at all, which
 * never happens for a real row but keeps this total rather than throwing. */
export function shortGameId(gameId) {
  return gameId ? String(gameId).slice(0, 8) : "-";
}

/** How long ago `iso` (a UTC ISO 8601 timestamp, or falsy/unparseable) was,
 * relative to `nowMs` - `null` when there is nothing to show. `nowMs` is a
 * parameter rather than read from the wall clock so this stays a pure,
 * deterministic function: `ui/render/lobby.js` calls it with `Date.now()`,
 * `tests/test_ui_lobby.py` calls it with a fixed instant.
 *
 * A negative age (the timestamp is technically in the future - clock skew
 * between this browser and the server) reads the same as "just now" rather
 * than a nonsensical negative duration. Past 30 days this stops answering
 * "how long ago" at all and gives the plain date instead - by then that is
 * the more useful thing to scan for. */
export function relativeTime(iso, nowMs = Date.now()) {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const diffSec = Math.max(0, Math.round((nowMs - then) / 1000));
  if (diffSec < 45) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return new Date(then).toISOString().slice(0, 10);
}

/** The one timestamp a row has to show, whichever it is: `createdAt` for a
 * row the server still lists (a plain column, see `decks_api.
 * LobbyGameSummary.created_at`), or `endedAt` for a row only `mcm.games.v1`
 * still remembers - `list_lobby_games` drops an archived game outright, so
 * such a row never carries a `created_at` at all. `null` when neither is
 * known. */
export function rowTimestamp(row) {
  return (row && (row.createdAt || row.endedAt)) || null;
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
    // Never known for an ended row - `list_lobby_games` drops it outright,
    // so nothing here ever saw its `created_at`. `rowTimestamp` falls back
    // to `endedAt` below for exactly this row.
    createdAt: null,
    endedAt: entry.endedAt || null,
    // An archived row is never "recording" - `gameState` checks `archived`
    // before it ever looks at this field, but it is set explicitly (never
    // left `undefined`) so every row this module hands back has the same
    // shape.
    postGameWork: false,
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
    .map((s) => archivedRow(s))
    // Newest-ended first, same direction as the live rows above them
    // (`decks_api.list_lobby_games` now sorts `created_at` descending) -
    // the server has nothing left to sort these by, so this is the one
    // ordering `lobbyViewModel` does itself.
    .sort((a, b) => (b.endedAt || "").localeCompare(a.endedAt || ""));
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
        // A plain column (`decks_api.LobbyGameSummary.created_at`) - the
        // row's own "when", and (with the server's own newest-first sort)
        // what lets a person actually find the game they were just playing
        // instead of guessing between two otherwise-identical rows.
        createdAt: g.created_at || null,
        endedAt: null,
        // Only ever true on a finished row (`decks_api.LobbyGameSummary.
        // post_game_work`, A3): the seat pod is still writing its guides/
        // reflection and has not released itself yet, so `gameState` reads
        // this alongside `over` to show "recording plays..." rather than
        // Replay/Forget while that work is in flight.
        postGameWork: Boolean(g.post_game_work),
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
