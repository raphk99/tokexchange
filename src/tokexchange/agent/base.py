from __future__ import annotations

import threading
from pathlib import Path

from ..models import AgentRunResult, AgentSpec


class AgentError(RuntimeError):
    pass


class Agent:
    kind = "abstract"

    def preflight(self) -> list[str]:
        """Return a list of problems (empty when the agent is ready to run)."""
        return []

    def run(self, *, workspace: Path, prompt: str, brief_file: Path, spec: AgentSpec, log_dir: Path,
            transcript_file: Path | None = None, test_commands: list[str] | None = None,
            cancel_event: threading.Event | None = None) -> AgentRunResult:
        """Run the agent in ``workspace`` and return a structured result.

        ``cancel_event`` is set by the worker when the submitter cancels the task; long-running
        agents should poll it and stop.
        """
        raise NotImplementedError
