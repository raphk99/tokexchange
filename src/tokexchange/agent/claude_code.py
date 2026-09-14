"""Run Claude Code non-interactively (``claude -p``) inside a workspace.

Authentication facts (verified against Claude Code 2.1.268 and the official docs):

* ``claude -p`` uses whatever credential the machine already has, in this
  precedence: cloud provider vars, ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_API_KEY``,
  ``apiKeyHelper``, ``CLAUDE_CODE_OAUTH_TOKEN`` (from ``claude setup-token``),
  Anthropic profiles, and finally the subscription OAuth login from ``/login``.
  A Pro/Max login therefore works for non-interactive runs on the machine that
  performed the login.
* ``--bare`` never reads OAuth credentials, so this adapter does not use it.
* Never copy credentials between machines: each laptop uses its own login.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..models import AgentRunResult, AgentSpec
from .base import Agent, AgentError

READ_ONLY_GIT = ["Bash(git status *)", "Bash(git status)", "Bash(git diff *)", "Bash(git diff)", "Bash(git log *)",
                 "Bash(git show *)", "Bash(git ls-files *)", "Bash(git blame *)", "Bash(git grep *)"]
DEFAULT_DISALLOWED = ["Bash(git push *)", "Bash(git remote *)", "Bash(git fetch *)", "Bash(git pull *)",
                      "Bash(gh pr *)", "Bash(gh repo *)"]


class ClaudeCodeAgent(Agent):
    kind = "claude-code"

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("TOKEXCHANGE_CLAUDE_BIN") or "claude"

    def preflight(self) -> list[str]:
        problems = []
        if shutil.which(self.binary) is None:
            problems.append(f"claude binary not found: {self.binary}")
            return problems
        proc = subprocess.run([self.binary, "auth", "status"], capture_output=True, text=True)
        try:
            status = json.loads(proc.stdout)
            if not status.get("loggedIn"):
                problems.append("claude is not logged in on this machine (run `claude` once and log in, or set CLAUDE_CODE_OAUTH_TOKEN)")
        except json.JSONDecodeError:
            if proc.returncode != 0:
                problems.append(f"`claude auth status` failed: {proc.stderr.strip()[:200]}")
        return problems

    def build_command(self, *, prompt: str, brief_file: Path, spec: AgentSpec, transcript_file: Path | None,
                      session_name: str, test_commands: list[str]) -> list[str]:
        cmd = [self.binary, "-p", prompt, "--output-format", "json", "--permission-prompts", "none",
               "--max-turns", str(spec.max_turns), "--append-system-prompt-file", str(brief_file), "--name", session_name]
        if spec.permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd += ["--permission-mode", spec.permission_mode]
        if spec.model:
            cmd += ["--model", spec.model]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        allowed = [*READ_ONLY_GIT, *spec.allowed_tools]
        for t in test_commands:
            allowed += [f"Bash({t})", f"Bash({t} *)"]
        if allowed:
            cmd += ["--allowedTools", ",".join(dict.fromkeys(allowed))]
        disallowed = [*DEFAULT_DISALLOWED, *spec.disallowed_tools]
        cmd += ["--disallowedTools", ",".join(dict.fromkeys(disallowed))]
        if spec.context_strategy == "resume-transcript" and transcript_file is not None:
            # Experimental: replay the submitter's transcript as conversation history.
            cmd += ["--resume", str(transcript_file), "--fork-session"]
        return cmd

    def run(self, *, workspace: Path, prompt: str, brief_file: Path, spec: AgentSpec, log_dir: Path,
            transcript_file: Path | None = None, test_commands: list[str] | None = None,
            cancel_event: threading.Event | None = None) -> AgentRunResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path, result_path = log_dir / "agent_stdout.json", log_dir / "agent_stderr.log", log_dir / "agent_result.json"
        cmd = self.build_command(prompt=prompt, brief_file=brief_file, spec=spec, transcript_file=transcript_file,
                                 session_name=f"tokexchange-{log_dir.parent.name[:8]}", test_commands=test_commands or [])
        (log_dir / "agent_command.txt").write_text("\n".join(cmd))
        env = dict(os.environ)
        # Running inside another Claude Code session must not confuse the child.
        for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION"):
            env.pop(var, None)
        timed_out = False
        start = time.monotonic()
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(cmd, cwd=str(workspace), stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                                    start_new_session=True)
            deadline = start + spec.timeout_seconds
            cancelled = False
            while True:
                try:
                    proc.wait(timeout=min(5.0, max(0.1, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
            if timed_out or cancelled:
                # SIGINT ends the turn cleanly; SIGTERM as a fallback.
                for sig, grace in ((signal.SIGINT, 20), (signal.SIGTERM, 10)):
                    try:
                        os.killpg(proc.pid, sig)
                        proc.wait(timeout=grace)
                        break
                    except (subprocess.TimeoutExpired, ProcessLookupError):
                        continue
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        result = AgentRunResult(kind=self.kind, exit_code=proc.returncode, timed_out=timed_out,
                                stdout_file=stdout_path.name, stderr_file=stderr_path.name)
        try:
            payload = json.loads(stdout_path.read_text() or "null")
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            result_path.write_text(json.dumps(payload, indent=2))
            result.result_json_file = result_path.name
            result.is_error = bool(payload.get("is_error")) or payload.get("subtype") not in (None, "success")
            result.session_id = payload.get("session_id")
            result.num_turns = payload.get("num_turns")
            result.cost_usd_estimate = payload.get("total_cost_usd")
            result.permission_denials = len(payload.get("permission_denials") or [])
            result.summary = str(payload.get("result") or "")[:20000]
        else:
            result.is_error = True
            tail = stderr_path.read_text(errors="replace")[-2000:]
            result.summary = f"claude produced no JSON result (exit {proc.returncode}, {time.monotonic() - start:.0f}s). stderr tail:\n{tail}"
        return result
