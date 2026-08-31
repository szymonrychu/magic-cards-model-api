"""The driver's container wiring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DriverSettings:
    game_api: str = "http://game:8000"
    wrapper_api: str = "http://agent:8080"
    wrapper_secret: str = ""
    guides_root: Path = Path("/guides")
    memory_root: Path = Path("/workspace/memory")
    workspace: Path = Path("/workspace")
    self_url: str = "http://driver:8090"
    http_addr: str = ":8090"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DriverSettings:
        def value(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        return cls(
            game_api=value("GAME_API", "http://game:8000"),
            wrapper_api=value("WRAPPER_API", "http://agent:8080"),
            wrapper_secret=value("WRAPPER_SHARED_SECRET", ""),
            guides_root=Path(value("GUIDES_ROOT", "/guides")),
            memory_root=Path(value("MEMORY_ROOT", "/workspace/memory")),
            workspace=Path(value("DRIVER_WORKSPACE", "/workspace")),
            self_url=value("DRIVER_SELF_URL", "http://driver:8090"),
            http_addr=value("HTTP_ADDR", ":8090"),
        )
