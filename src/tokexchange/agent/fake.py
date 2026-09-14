"""A deterministic stand-in agent used by the test-suite and for dry runs.

It reads the prompt for a line of the form ``FAKE: write <path> <<text>>``
(repeatable) and performs those writes; otherwise it appends a note to
``FAKE_AGENT.md``. This exercises the full pipeline without any model calls.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import AgentRunResult, AgentSpec
from .base import Agent

_CMD = re.compile(r"^FAKE: write (\S+) <<(.*)>>$", re.M)


class FakeAgent(Agent):
    kind = "fake"

    def run(self, *, workspace: Path, prompt: str, brief_file: Path, spec: AgentSpec, log_dir: Path,
            transcript_file: Path | None = None, test_commands: list[str] | None = None,
            cancel_event=None) -> AgentRunResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        writes = _CMD.findall(prompt)
        if writes:
            for rel, text in writes:
                target = workspace / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text.replace("\\n", "\n"))
        elif "FAKE: nothing" in prompt:
            pass
        else:
            with (workspace / "FAKE_AGENT.md").open("a") as fh:
                fh.write(f"Fake agent saw request: {prompt.splitlines()[0][:120]}\n")
        (log_dir / "agent_result.json").write_text(json.dumps({"result": "fake run complete", "brief_bytes": brief_file.stat().st_size}))
        return AgentRunResult(kind=self.kind, exit_code=0, summary="fake run complete", num_turns=1,
                              result_json_file="agent_result.json", is_error="FAKE: fail" in prompt)
