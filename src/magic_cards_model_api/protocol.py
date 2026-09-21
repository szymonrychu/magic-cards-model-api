"""The wire-contract version this api build speaks - owned here, not borrowed.

`/api/health` used to answer with `seatkit.PROTOCOL_VERSION`, imported
straight out of the mcp toolset. That was backwards: the api is the side
every other service depends on, and `seatkit.py`/`mcp.py`/`seat_cli.py` are
moving to their own repo (`magic-cards-mcp`, 2b's decomposition map) where
they cannot import this package at all. This module is the api's own,
canonical copy - `app.py`'s `/api/health` handler reads it from here.

`seatkit.py` keeps an independent copy of the same string for its own
`api_trouble` staleness check (it compares what `/api/health` reports against
its own build's expectation - see that module's docstring for why the check
exists and why it must be a value baked into the MCP process, not one fetched
at the moment of the comparison). The two literals have to be bumped together
by hand from here on, the same way `agent_settings.py`'s cross-repo spellings
already are (see MEMORY.md, 2026-09-08, "`agent_settings.py` crosses four
repos in three spellings") - there is no import left to keep them honest for
free once the repos split, and `tests/test_protocol_boundary.py` is the
standing pin that catches one side moving without the other.

Bump this alongside any change to a tool's request/response shape - see
`seatkit.PROTOCOL_VERSION`'s own docstring for what has moved it before.
"""

from __future__ import annotations

PROTOCOL_VERSION = "seatkit-21"
