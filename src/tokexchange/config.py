"""Configuration: ``~/.config/tokexchange/config.json`` with environment overrides.

    {
      "coordinator_url": "https://coord.example.com",
      "token": "…",                # bearer token for this device
      "secret": "…",               # optional shared secret for end-to-end encryption
      "worker": {"id": "laptop-b", "work_root": "~/tokexchange-work", "concurrency": 1,
                 "poll_interval": 15, "agent": "claude-code", "keep_workspaces": false},
      "submit": {"max_turns": 60, "timeout_seconds": 2400, "permission_mode": "acceptEdits",
                 "model": null, "effort": null, "pushed_only": false}
    }

Environment: ``TOKEXCHANGE_URL``, ``TOKEXCHANGE_TOKEN``, ``TOKEXCHANGE_SECRET``, ``TOKEXCHANGE_CONFIG``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def config_path() -> Path:
    return Path(os.environ.get("TOKEXCHANGE_CONFIG") or Path.home() / ".config" / "tokexchange" / "config.json")


def state_dir() -> Path:
    p = Path(os.environ.get("TOKEXCHANGE_STATE_DIR") or Path.home() / ".local" / "share" / "tokexchange")
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Config:
    coordinator_url: str | None = None
    token: str | None = None
    secret: str | None = None
    worker: dict[str, Any] = field(default_factory=dict)
    submit: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        data: dict[str, Any] = {}
        if path.is_file():
            data = json.loads(path.read_text())
        cfg = cls(coordinator_url=data.get("coordinator_url"), token=data.get("token"), secret=data.get("secret"),
                  worker=dict(data.get("worker") or {}), submit=dict(data.get("submit") or {}), path=path)
        cfg.coordinator_url = os.environ.get("TOKEXCHANGE_URL") or cfg.coordinator_url
        cfg.token = os.environ.get("TOKEXCHANGE_TOKEN") or cfg.token
        cfg.secret = os.environ.get("TOKEXCHANGE_SECRET") or cfg.secret
        return cfg

    def save(self) -> None:
        path = self.path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"coordinator_url": self.coordinator_url, "token": self.token, "secret": self.secret,
                "worker": self.worker, "submit": self.submit}
        path.write_text(json.dumps({k: v for k, v in data.items() if v not in (None, {})}, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
