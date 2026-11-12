# Task 18 report

Changes: README "Chat simulation MCP" section (tool list incl. import and Claude tools, MTGO import limits and board_caveats, Claude matches, env caps table from sim/config.py, preset mcp_servers entry, agent usage guide); deploy/k8s/README.md section for Secret mtg-sim creation and rotation (no value shown); MEMORY.md entry; test_readme_lists_every_sim_tool.

TDD: RED `uv run --python 3.13 pytest -o addopts= -q tests/test_sim_server.py -k readme` -> 1 failed (tools missing from README section). GREEN: full file 17 passed; `mise run lint` all checks passed; gitleaks no leaks.

Concerns: MTG_SIM_MATCH_WALL_S documented as 300, env-set, unsettled (Q7). Vault re-pin behaviour on rotation stated cautiously, unverified. Removed the "full guide lands with S7" line. Untracked tests/fixtures/scryfall files pre-existed, not committed.

## Fix round 1

All 3 findings fixed: rotation paragraph rewritten (vault PUT /vault/v1/entries/{id}/value, pin at create, old sessions 401 until recreated); single idempotent create/rotate block with unset; README no longer claims session_patch. MEMORY.md had no wrong claim. tests/test_sim_server.py 17 passed, lint clean.
