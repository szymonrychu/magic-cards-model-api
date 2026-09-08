// The two origins this deployment's API calls resolve against: `apiBase`
// for the game-play API (`/api/games/...`, `gamePath()` in `app.js`) and
// `lobbyBase` for the lobby-facing one (`/api/ui/...` - deck picking, the
// games table, the AI seat, symbology - the map's own split between
// `magic-cards-api` and `magic-cards-lobby`). Both default to `/api`,
// origin-absolute rather than resolved against `document.baseURI`: the API
// lives at exactly one place regardless of which page path (`/`,
// `/g/<uuid>/`) served this script, so a leading `/` stays correct once
// `/g/<uuid>/` addressing exists (T4) and asset requests start resolving
// relative to that path instead.
//
// M5's static frontend container mounts a ConfigMap-generated `/config.json`
// naming both explicitly. This repo, still serving the API and the UI from
// one origin, ships no such file - the fetch's 404 (or any other failure:
// unreachable, unparsable) leaves the defaults standing, which is exactly
// what keeps `mise run run` working with no config file at all.

let apiBaseValue = "/api";
let lobbyBaseValue = "/api";
let loaded = null;

export async function loadConfig(fetchImpl = fetch) {
  if (loaded) return loaded;
  loaded = (async () => {
    try {
      const res = await fetchImpl("/config.json");
      if (res.ok) {
        const json = await res.json();
        if (json.apiBase) apiBaseValue = json.apiBase;
        if (json.lobbyBase) lobbyBaseValue = json.lobbyBase;
      }
    } catch {
      // absent, unreachable, or unparsable - the defaults stand
    }
    return { apiBase: apiBaseValue, lobbyBase: lobbyBaseValue };
  })();
  return loaded;
}

export function apiBase() {
  return apiBaseValue;
}

export function lobbyBase() {
  return lobbyBaseValue;
}
