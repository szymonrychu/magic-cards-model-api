"""Render one seat Job and one buddy (advisor) Job to stdout, for `mise run k8s:render`.

Goes through the same `build_job` the api uses at runtime, so the output shows
exactly what differs between the two roles: model (seat Sonnet, buddy Opus via
`ADVISOR_MODEL`), `MTG_ROLE`, the role label and the strict-baton flag.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

import yaml

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import ROLE_ADVISOR, ROLE_SEAT, K8sSettings, build_job


def render_jobs(env: Mapping[str, str]) -> list[dict[str, Any]]:
    settings = K8sSettings.from_env(env)
    common = {"game_id": "example-game", "uid": "example-seat", "player": "example"}
    seat = build_job(settings, agent=AgentSettings.default(env), role=ROLE_SEAT, **common)
    buddy = build_job(
        settings, agent=AgentSettings.advisor_default(env), role=ROLE_ADVISOR, **common
    )
    return [seat, buddy]


def main() -> None:
    for job in render_jobs(os.environ):
        sys.stdout.write("---\n" + yaml.safe_dump(job, sort_keys=False))


if __name__ == "__main__":
    main()
