// The one place the browser talks to the game server. Moved out of `app.js`
// unchanged so the store, the pure view-model modules, and `app.js` itself
// all go through the same fetch wrapper.

export async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let json;
  try {
    json = await res.json();
  } catch {
    json = { detail: `HTTP ${res.status}: could not parse response` };
  }
  if (!res.ok && json && json.detail !== undefined) {
    const message = Array.isArray(json.detail)
      ? json.detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("; ")
      : json.detail;
    const err = new Error(message);
    err.status = res.status;
    err.detail = json.detail;
    throw err;
  }
  return json;
}

export function withUid(path, uid) {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}uid=${encodeURIComponent(uid)}`;
}
