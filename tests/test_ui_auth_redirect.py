"""`ui/api.js`'s login-redirect handling under Node (2b's oauth2-proxy gate).

oauth2-proxy is configured with `--api-route=^/api/`: a browser navigation to
`/` that is not signed in gets a 302 to Keycloak, but an XHR to `/api/*` gets
a plain 401 with no redirect. `api()` is the single fetch wrapper every UI
caller goes through (`app.js`'s poll loops, `lobby.js`'s refreshes), so
teaching it to redirect on 401/403 fixes every one of those call sites
without touching them - see `ui/api.js`'s module docstring.

Each test spawns its own Node process (`run_module` in `tests/ui_node.py`),
so the module's "already navigating" latch starts fresh every time except
where a single script deliberately fires several `api()` calls at once
(the concurrent-401 test) to prove the latch does its job.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

MODULE = "api.js"


def _fetch_returning(status: int, ok: bool, body: object) -> str:
    """A `globalThis.fetch` stub resolving to one fixed JSON response."""
    import json as _json

    return f"""async () => ({{
      status: {status},
      ok: {str(ok).lower()},
      json: async () => ({_json.dumps(body)}),
    }})"""


def _fetch_non_json(status: int, ok: bool) -> str:
    """A `globalThis.fetch` stub whose `.json()` rejects - a same-origin HTML
    login page, or any other non-JSON 2xx body."""
    return f"""async () => ({{
      status: {status},
      ok: {str(ok).lower()},
      json: async () => {{ throw new Error("Unexpected token '<'"); }},
    }})"""


def _fake_window(pathname: str = "/", search: str = "") -> str:
    return f"""{{
      location: {{
        pathname: {pathname!r},
        search: {search!r},
        assign: (url) => calls.push(url),
      }},
    }}"""


def test_a_401_navigates_to_oauth2_start_and_throws_auth_required() -> None:
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(401, False, {"detail": "nope"})};
          const calls = [];
          globalThis.window = {_fake_window("/", "")};
          let errName = null, errStatus = null;
          try {{
            await m.api("GET", "/api/lobby/games");
          }} catch (e) {{
            errName = e.name;
            errStatus = e.status;
          }}
          return {{ calls, errName, errStatus }};
        }})()""",
    )
    assert result["calls"] == ["/oauth2/start?rd=%2F"]
    assert result["errName"] == "AuthRequiredError"
    assert result["errStatus"] == 401


def test_a_403_also_navigates_to_oauth2_start_and_throws_auth_required() -> None:
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(403, False, {"detail": "forbidden"})};
          const calls = [];
          globalThis.window = {_fake_window("/", "")};
          let errName = null, errStatus = null;
          try {{
            await m.api("GET", "/api/lobby/games");
          }} catch (e) {{
            errName = e.name;
            errStatus = e.status;
          }}
          return {{ calls, errName, errStatus }};
        }})()""",
    )
    assert result["calls"] == ["/oauth2/start?rd=%2F"]
    assert result["errName"] == "AuthRequiredError"
    assert result["errStatus"] == 403


def test_rd_is_percent_encoded_for_a_game_path_with_a_query_string() -> None:
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(401, False, {"detail": "nope"})};
          const calls = [];
          globalThis.window = {_fake_window("/g/abc-123/", "?x=1")};
          try {{ await m.api("GET", "/api/games/abc-123"); }} catch {{}}
          return calls;
        }})()""",
    )
    assert result == ["/oauth2/start?rd=%2Fg%2Fabc-123%2F%3Fx%3D1"]


def test_three_concurrent_401s_navigate_exactly_once() -> None:
    """Mirrors the board poll (1.2s), the seat poll (5s) and a lobby refresh
    all hitting a 401 in the same tick."""
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(401, False, {"detail": "nope"})};
          const calls = [];
          globalThis.window = {_fake_window("/g/xyz/", "")};
          const settled = await Promise.allSettled([
            m.api("GET", "/api/a"),
            m.api("GET", "/api/b"),
            m.api("GET", "/api/c"),
          ]);
          return {{
            callCount: calls.length,
            allRejected: settled.every((r) => r.status === "rejected"),
            allAuthRequired: settled.every(
              (r) => r.reason && r.reason.name === "AuthRequiredError"
            ),
          }};
        }})()""",
    )
    assert result["callCount"] == 1, "three simultaneous 401s must only navigate once"
    assert result["allRejected"] is True
    assert result["allAuthRequired"] is True


def test_a_200_with_json_body_still_returns_it_and_does_not_navigate() -> None:
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(200, True, {"hello": "world"})};
          const calls = [];
          globalThis.window = {_fake_window("/", "")};
          const data = await m.api("GET", "/api/x");
          return {{ data, calls }};
        }})()""",
    )
    assert result["data"] == {"hello": "world"}
    assert result["calls"] == []


def test_a_500_with_fastapi_detail_throws_with_that_message_and_does_not_navigate() -> None:
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(500, False, {"detail": "boom"})};
          const calls = [];
          globalThis.window = {_fake_window("/", "")};
          let message = null;
          try {{ await m.api("GET", "/api/x"); }} catch (e) {{ message = e.message; }}
          return {{ message, calls }};
        }})()""",
    )
    assert result["message"] == "boom"
    assert result["calls"] == []


def test_a_422_validation_array_is_formatted_loc_dot_msg_joined_by_semicolons() -> None:
    """Pins the exact formatting `ui/render/lobby.js` and friends render to
    the player - do not change it while fixing the surrounding defects."""
    detail = [
        {"loc": ["body", "text"], "msg": "field required"},
        {"loc": ["query", "uid"], "msg": "invalid"},
    ]
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(422, False, {"detail": detail})};
          globalThis.window = {_fake_window("/", "")};
          const calls = [];
          let message = null;
          try {{
            await m.api("POST", "/api/decks/validate", {{}});
          }} catch (e) {{
            message = e.message;
          }}
          return message;
        }})()""",
    )
    assert result == "body.text: field required; query.uid: invalid"


def test_a_response_with_no_detail_key_throws_instead_of_returning_the_body_as_data() -> None:
    """Defect 1: the old code only threw when `!res.ok && json.detail !==
    undefined` - a non-FastAPI-shaped error body silently fell through to
    `return json`, handing a failed request back as if it were game state."""
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_returning(500, False, {"whoops": "no detail field"})};
          globalThis.window = {_fake_window("/", "")};
          const calls = [];
          let threw = false, message = null;
          try {{
            await m.api("GET", "/api/x");
          }} catch (e) {{
            threw = true;
            message = e.message;
          }}
          return {{ threw, message, calls }};
        }})()""",
    )
    assert result["threw"] is True
    assert result["message"] == "HTTP 500"
    assert result["calls"] == []


def test_a_200_with_non_json_body_throws_instead_of_being_returned_as_state() -> None:
    """Defect 2: a same-origin HTML login page returns 200; the old code's
    `catch` built a `{detail: "HTTP 200: ..."}` sentinel and `!res.ok` being
    false meant it was `return`ed as if it were a real state view."""
    result = run_module(
        MODULE,
        f"""(async () => {{
          globalThis.fetch = {_fetch_non_json(200, True)};
          globalThis.window = {_fake_window("/", "")};
          const calls = [];
          let threw = false, isAuthRequired = false;
          try {{
            await m.api("GET", "/api/x");
          }} catch (e) {{
            threw = true;
            isAuthRequired = e.name === "AuthRequiredError";
          }}
          return {{ threw, isAuthRequired, calls }};
        }})()""",
    )
    assert result["threw"] is True
    assert result["isAuthRequired"] is False
    assert result["calls"] == []


def test_login_url_is_a_pure_function_of_pathname_and_search() -> None:
    result = run_module(
        MODULE,
        """m.loginUrl({ pathname: "/g/abc-123/", search: "?x=1" })""",
    )
    assert result == "/oauth2/start?rd=%2Fg%2Fabc-123%2F%3Fx%3D1"


def test_go_to_login_is_a_safe_no_op_with_no_window() -> None:
    result = run_module(
        MODULE,
        """m.goToLogin()""",
    )
    assert result is False
