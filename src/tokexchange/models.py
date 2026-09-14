"""Data models for tasks, manifests and execution reports.

Everything that crosses a machine boundary is a plain JSON document with a
``schema_version`` so that the format can evolve. Dataclasses here are thin
wrappers around those documents; ``to_dict``/``from_dict`` are the only
serialisation path.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Task lifecycle states as stored by the coordinator.
STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED}

# Outcome values written by the worker into the execution report.
OUTCOME_SUCCESS = "success"          # agent finished, tests passed (or none declared)
OUTCOME_TESTS_FAILED = "tests_failed"  # agent finished, patch produced, tests failed
OUTCOME_AGENT_ERROR = "agent_error"  # agent exited with error / is_error
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NO_CHANGES = "no_changes"
OUTCOME_WORKSPACE_ERROR = "workspace_error"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def new_task_id() -> str:
    return str(uuid.uuid4())


@dataclass
class RepoRef:
    """Where the code lives and which exact revision the task is based on."""

    remote_url: str | None
    branch: str
    base_commit: str
    has_uncommitted_patch: bool = False
    uncommitted_patch_sha256: str | None = None
    untracked_files: list[str] = field(default_factory=list)
    has_commit_bundle: bool = False
    commit_bundle_ref: str = "refs/tokexchange/base"


@dataclass
class Acceptance:
    tests: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    timeout_seconds: int = 900  # per test command


@dataclass
class AgentSpec:
    kind: str = "claude-code"
    model: str | None = None
    effort: str | None = None
    max_turns: int = 60
    timeout_seconds: int = 2400
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    context_strategy: str = "brief"  # "brief" | "resume-transcript" (experimental)


@dataclass
class Limits:
    max_attempts: int = 2
    lease_seconds: int = 3600


@dataclass
class ContextRef:
    brief_file: str = "context.md"
    transcript_file: str | None = None
    attached_files: list[str] = field(default_factory=list)
    source_session_id: str | None = None


@dataclass
class TaskManifest:
    """The ``task.json`` document inside a task bundle."""

    task_id: str
    title: str
    request: str
    repo: RepoRef
    acceptance: Acceptance = field(default_factory=Acceptance)
    constraints: list[str] = field(default_factory=list)
    context: ContextRef = field(default_factory=ContextRef)
    agent: AgentSpec = field(default_factory=AgentSpec)
    limits: Limits = field(default_factory=Limits)
    created_at: str = field(default_factory=now_iso)
    created_by: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskManifest":
        version = d.get("schema_version", 1)
        if version > SCHEMA_VERSION:
            raise ValueError(f"task schema_version {version} is newer than supported {SCHEMA_VERSION}")
        return cls(
            task_id=d["task_id"],
            title=d["title"],
            request=d["request"],
            repo=RepoRef(**d["repo"]),
            acceptance=Acceptance(**d.get("acceptance", {})),
            constraints=list(d.get("constraints", [])),
            context=ContextRef(**d.get("context", {})),
            agent=AgentSpec(**d.get("agent", {})),
            limits=Limits(**d.get("limits", {})),
            created_at=d.get("created_at", now_iso()),
            created_by=d.get("created_by"),
            schema_version=version,
        )

    def summary_meta(self) -> dict[str, Any]:
        """Small, non-sensitive metadata the coordinator is allowed to see in clear."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "base_commit": self.repo.base_commit,
            "branch": self.repo.branch,
            "agent_kind": self.agent.kind,
            "max_attempts": self.limits.max_attempts,
            "lease_seconds": self.limits.lease_seconds,
            "schema_version": self.schema_version,
        }


@dataclass
class TestRun:
    command: str
    exit_code: int | None
    duration_seconds: float
    log_file: str
    timed_out: bool = False


@dataclass
class AgentRunResult:
    kind: str
    exit_code: int | None
    timed_out: bool = False
    is_error: bool = False
    session_id: str | None = None
    num_turns: int | None = None
    cost_usd_estimate: float | None = None
    summary: str = ""
    stdout_file: str | None = None
    stderr_file: str | None = None
    result_json_file: str | None = None
    permission_denials: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class PatchInfo:
    file: str
    files_changed: int
    insertions: int
    deletions: int
    sha256: str
    full_patch_file: str | None = None
    changed_paths: list[str] = field(default_factory=list)


@dataclass
class ExecutionReport:
    """The ``report.json`` document inside a result bundle."""

    task_id: str
    outcome: str
    worker_id: str
    started_at: str
    finished_at: str
    base_commit: str
    baseline_commit: str | None = None
    agent: dict[str, Any] | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    patch: dict[str, Any] | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecutionReport":
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}})

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_SUCCESS
