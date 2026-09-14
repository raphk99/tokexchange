"""Context collection on the submitting laptop.

Claude Code keeps every session as a JSONL transcript under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. The format is
documented as internal and may change, so the parser here is deliberately
defensive: it only extracts human prompts and assistant prose and ignores
everything it does not recognise. The output is a Markdown "brief" that is
appended to the delegated agent's system prompt.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def encode_project_dir(cwd: Path | str) -> str:
    """Mirror Claude Code's project directory naming: non-alphanumerics become '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_transcript_dir(cwd: Path | str) -> Path:
    return claude_config_dir() / "projects" / encode_project_dir(cwd)


def find_transcript(cwd: Path | str, session_id: str | None = None) -> Path | None:
    """Locate a transcript for ``cwd``.

    Resolution order: explicit ``session_id``; the ``CLAUDE_CODE_SESSION_ID``
    environment variable (set when a shell command runs inside Claude Code);
    otherwise the most recently modified transcript for the directory.
    """
    directory = project_transcript_dir(cwd)
    if not directory.is_dir():
        return None
    candidates = [session_id, os.environ.get("CLAUDE_CODE_SESSION_ID")]
    for sid in candidates:
        if sid:
            p = directory / f"{sid}.jsonl"
            if p.is_file():
                return p
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


@dataclass
class Turn:
    role: str  # "user" | "assistant" | "summary"
    text: str
    timestamp: str | None = None


@dataclass
class Digest:
    turns: list[Turn] = field(default_factory=list)
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    source_path: str | None = None
    truncated: bool = False


def _text_of_content(content) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
    return "\n".join(p for p in parts if p)


_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _clean(text: str) -> str:
    text = _SYSTEM_REMINDER.sub("", text)
    return text.strip()


def parse_transcript(lines: Iterable[str]) -> Digest:
    digest = Digest()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        digest.session_id = digest.session_id or obj.get("sessionId")
        digest.cwd = digest.cwd or obj.get("cwd")
        digest.git_branch = digest.git_branch or obj.get("gitBranch")
        typ = obj.get("type")
        if typ not in ("user", "assistant"):
            continue
        if obj.get("isSidechain") or obj.get("isMeta"):
            continue
        message = obj.get("message") or {}
        content = message.get("content")
        if typ == "user":
            # Tool results are user-role messages whose blocks are tool_result; skip them.
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            origin = obj.get("origin") or {}
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                continue
        text = _clean(_text_of_content(content))
        if not text:
            continue
        role = "summary" if obj.get("isCompactSummary") else typ
        digest.turns.append(Turn(role=role, text=text, timestamp=obj.get("timestamp")))
    return digest


def load_transcript(path: Path) -> Digest:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        digest = parse_transcript(fh)
    digest.source_path = str(path)
    return digest


def budget_turns(turns: list[Turn], max_chars: int, per_turn_chars: int = 4000) -> tuple[list[Turn], bool]:
    """Keep the first user turn plus as many recent turns as fit in ``max_chars``.

    The first turn is usually the original request, so it may use up to half the budget
    before being clipped; later turns are clipped at ``per_turn_chars``.
    """
    if not turns:
        return [], False
    truncated = False
    clipped: list[Turn] = []
    for i, t in enumerate(turns):
        limit = max(per_turn_chars, max_chars // 2) if i == 0 else per_turn_chars
        if len(t.text) > limit:
            truncated = True
            clipped.append(Turn(t.role, t.text[:limit] + "\n[... truncated ...]", t.timestamp))
        else:
            clipped.append(t)
    first = clipped[0]
    remaining = max_chars - len(first.text)
    kept: list[Turn] = []
    for t in reversed(clipped[1:]):
        if len(t.text) > remaining:
            truncated = True
            break
        kept.append(t)
        remaining -= len(t.text)
    kept.reverse()
    return [first, *kept], truncated


def render_brief(*, request: str, repo: dict, digest: Digest | None, notes: list[str], criteria: list[str],
                 tests: list[str], constraints: list[str], attached_files: list[str], max_chars: int = 24000) -> str:
    out: list[str] = []
    out.append("# Delegated task brief")
    out.append("")
    out.append("You are executing a task delegated from another machine on behalf of the same developer. "
               "The developer will review your changes as a git patch; they are not available to answer questions. "
               "Make reasonable assumptions, state them in your final message, and do not stop early.")
    out.append("")
    out.append("## Repository")
    out.append(f"- Branch: `{repo.get('branch')}`")
    out.append(f"- Base commit: `{repo.get('base_commit')}`")
    if repo.get("has_uncommitted_patch") or repo.get("untracked_files"):
        out.append("- The developer's uncommitted changes have already been applied to this workspace as the baseline "
                   "commit. Build on top of them; do not revert them.")
    out.append("")
    if constraints:
        out.append("## Constraints")
        out.extend(f"- {c}" for c in constraints)
        out.append("")
    if criteria or tests:
        out.append("## Completion criteria")
        out.extend(f"- {c}" for c in criteria)
        for t in tests:
            out.append(f"- The command `{t}` must pass.")
        out.append("")
    if notes:
        out.append("## Notes and prior decisions from the developer")
        out.extend(f"- {n}" for n in notes)
        out.append("")
    if attached_files:
        out.append("## Attached files")
        out.append("Files the developer attached explicitly are under `.tokexchange/attached/` in the workspace:")
        out.extend(f"- `{p}`" for p in attached_files)
        out.append("")
    if digest and digest.turns:
        turns, truncated = budget_turns(digest.turns, max_chars=max_chars)
        out.append("## Conversation history (excerpt)")
        out.append("The developer had the following conversation with a coding agent before delegating. "
                   "Tool calls and outputs were removed; only prompts and replies are shown."
                   + (" The excerpt is truncated; the most recent turns are kept." if (truncated or digest.truncated) else ""))
        out.append("")
        for t in turns:
            label = {"user": "Developer", "assistant": "Agent", "summary": "Earlier conversation summary"}[t.role]
            out.append(f"### {label}")
            out.append(t.text)
            out.append("")
    out.append("## The request")
    out.append(request.strip())
    out.append("")
    return "\n".join(out)


def render_prompt(*, request: str, tests: list[str], criteria: list[str]) -> str:
    """The user-turn prompt handed to the delegated agent."""
    lines = [request.strip(), "", "Operating rules for this delegated run:",
             "- Work only inside the current working directory (a dedicated git worktree).",
             "- Do not push, do not create pull requests, and do not modify git remotes.",
             "- Commit or leave changes uncommitted as you prefer; a patch will be generated automatically.",
             "- Do not ask questions; nobody can answer. Decide and continue.",
             "- Finish with a short summary of what changed and anything the reviewer should check."]
    if tests:
        lines.append("- Before finishing, run: " + "; ".join(f"`{t}`" for t in tests) + " and make them pass.")
    if criteria:
        lines.append("- Completion criteria: " + " ".join(criteria))
    return "\n".join(lines)
