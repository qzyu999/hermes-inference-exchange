"""Validated, non-secret provider settings."""

import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    sell_enabled: bool = False
    idle_minutes: float = 5
    drain_seconds: float = 8
    startup_seconds: float = 180
    coordinator: str = "ws://localhost:8000/ws/provider"
    ie_python: str = "python"
    ie_repo: str = ""
    model: str = ""
    llama_server: str = ""
    mock: bool = False

    def validate(self):
        for name in ("sell_enabled", "mock"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name, maximum in (
            ("idle_minutes", 1440),
            ("drain_seconds", 20),
            ("startup_seconds", 600),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f"{name} must be positive and at most {maximum}")
        for name in ("coordinator", "ie_python", "ie_repo", "model", "llama_server"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        url = urlsplit(self.coordinator)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            url.scheme not in {"ws", "wss"}
            or not url.hostname
            or url.path != "/ws/provider"
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError("coordinator must be a credential-free /ws/provider URL")
        if not local and url.scheme != "wss":
            raise ValueError("Remote coordinators require wss")
        if self.mock and not local:
            raise ValueError("Mock serving requires a loopback coordinator")
        if self.sell_enabled:
            if not self.ie_python:
                raise ValueError("Set ie_python to the IE Python executable")
            if (
                not Path(self.ie_repo).is_dir()
                or not (Path(self.ie_repo) / "ocip_agent" / "agent.py").is_file()
            ):
                raise ValueError("Set ie_repo to an IE checkout")
            if not self.mock:
                if not self.model.lower().endswith(".gguf") or not Path(self.model).is_file():
                    raise ValueError("Set model to an existing local GGUF file")
                if not Path(self.llama_server).is_file():
                    raise ValueError("Set llama_server to an installed executable")
        return self
