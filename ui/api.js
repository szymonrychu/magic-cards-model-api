// The one place the browser talks to the game server. Moved out of `app.js`
// unchanged so the store, the pure view-model modules, and `app.js` itself
// all go through the same fetch wrapper.
//
// oauth2-proxy (2b) fronts this app with `--api-route=^/api/`: a browser
// navigation to `/` with no session gets a 302 to Keycloak, but an XHR to
// `/api/*` gets a plain 401/403 with no redirect. Handling that here, once,
// means every caller - `app.js`'s two poll loops (one swallows its error
// entirely) and `lobby.js`'s refreshes - starts redirecting to the login
// screen with no changes of their own.

/** Thrown instead of a plain `Error` when `api()` sees a 401/403, so a
 * caller that wants to tell "not signed in" apart from an ordinary refusal
 * can `instanceof` it. Nothing currently does; `goToLogin` has already fired
 * by the time this reaches a `catch`, so most callers just let it be one
 * more discarded rejection on the way to the browser leaving the page. */
export class AuthRequiredError extends Error {
  constructor(status) {
    super("not signed in");
    this.name = "AuthRequiredError";
    this.status = status;
  }
}

/** oauth2-proxy's documented URL to start the OAuth cycle
 * (https://oauth2-proxy.github.io/oauth2-proxy/features/endpoints/,
 * `/oauth2/start`), with `rd` set to the current same-origin path so login
 * returns here rather than to `/`. Kept relative on purpose - an absolute
 * URL would need `--whitelist-domain` on the proxy and buys nothing since
 * this app is always same-origin with itself. */
export function loginUrl(location) {
  return `/oauth2/start?rd=${encodeURIComponent(location.pathname + location.search)}`;
}

// Guards against the board poll (1.2s), the seat poll (5s) and a lobby
// refresh all hitting a 401 in the same tick and each trying to navigate -
// only the first should fire; a second `location.assign` while the first is
// still in flight is a wasted navigation at best.
let redirecting = false;

/** Reset the "already navigating to login" latch. Test-only - a real page
 * never needs this because a navigation away ends it anyway. */
export function resetAuthRedirect() {
  redirecting = false;
}

/** Navigate to the login screen, at most once. No-op (returns `false`)
 * outside a browser - Node tests, or any non-browser import of this module -
 * rather than throwing on a missing `window`. */
export function goToLogin(win) {
  const w = win !== undefined ? win : typeof window === "undefined" ? undefined : window;
  if (!w || redirecting) return false;
  redirecting = true;
  w.location.assign(loginUrl(w.location));
  return true;
}

export async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 || res.status === 403) {
    goToLogin();
    throw new AuthRequiredError(res.status);
  }
  let json;
  let parseFailed = false;
  try {
    json = await res.json();
  } catch {
    parseFailed = true;
  }
  if (!res.ok) {
    const detail = !parseFailed && json && json.detail !== undefined ? json.detail : `HTTP ${res.status}`;
    const message = Array.isArray(detail)
      ? detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("; ")
      : detail;
    const err = new Error(message);
    err.status = res.status;
    err.detail = !parseFailed ? json.detail : undefined;
    throw err;
  }
  // A 2xx whose body is not JSON (a same-origin HTML page, most likely a
  // login screen a redirect elsewhere hasn't caught) used to come back as a
  // `{detail: "HTTP 200: ..."}` sentinel handed to the caller as if it were
  // real data - `ui/render/board.js` then threw reaching into it for
  // `state.players`. Every current 2xx route in this api - GET, POST, and
  // every DELETE (`end_lobby_game`, `delete_game`) included - returns a real
  // JSON body, so there is no legitimate empty-body case to special-case
  // here; a caller that needs one should get `{}` from the endpoint, not a
  // silently-invented one here.
  if (parseFailed) {
    throw new Error(`HTTP ${res.status}: could not parse response`);
  }
  return json;
}

export function withUid(path, uid) {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}uid=${encodeURIComponent(uid)}`;
}
