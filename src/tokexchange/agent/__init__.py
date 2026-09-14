"""Coding-agent adapters. ``get_agent(kind)`` returns an :class:`Agent` implementation."""
from __future__ import annotations

from .base import Agent, AgentError


def get_agent(kind: str) -> Agent:
    if kind == "claude-code":
        from .claude_code import ClaudeCodeAgent
        return ClaudeCodeAgent()
    if kind == "fake":
        from .fake import FakeAgent
        return FakeAgent()
    raise AgentError(f"unknown agent kind: {kind}")


__all__ = ["Agent", "AgentError", "get_agent"]
