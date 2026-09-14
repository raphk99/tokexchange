"""Submitter side (Laptop A): turn a request plus repository state into a task bundle."""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import bundle as bundlemod
from . import context as ctx
from . import gitutil
from .config import state_dir
from .crypto import Sealer
from .models import (Acceptance, AgentSpec, ContextRef, Limits, RepoRef, TaskManifest, new_task_id)
from .transport import Transport

MAX_UNTRACKED_FILE_BYTES = 5 * 1024 * 1024
MAX_UNTRACKED_FILES = 500


@dataclass
class SubmitOptions:
    request: str
    title: str | None = None
    tests: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attach: list[str] = field(default_factory=list)
    session: str = "auto"            # "auto" | "none" | <session id> | <path to .jsonl>
    include_transcript: bool = False  # ship the raw transcript (for the experimental resume strategy)
    include_commits: str = "auto"     # "auto" | "yes" | "no"
    include_untracked: bool = True
    pushed_only: bool = False         # base the task on the pushed commit only; ship no local changes
    max_context_chars: int = 24000
    agent: AgentSpec = field(default_factory=AgentSpec)
    limits: Limits = field(default_factory=Limits)
    test_timeout_seconds: int = 900


@dataclass
class BuiltTask:
    manifest: TaskManifest
    bundle: bytes
    brief: str
    prompt: str
    warnings: list[str] = field(default_factory=list)


def _title_from_request(request: str) -> str:
    first = request.strip().splitlines()[0] if request.strip() else "untitled"
    return first[:72]


def build_task(cwd: Path, opts: SubmitOptions) -> BuiltTask:
    warnings: list[str] = []
    state = gitutil.inspect_repo(cwd)
    root = state.root
    entries: dict[str, bytes | Path] = {}

    # 0. Pushed-only mode: nothing local leaves this machine ---------------------
    if opts.pushed_only:
        if not state.remote_url:
            raise gitutil.GitError("--pushed-only needs a git remote the other laptop can clone")
        if not gitutil.commit_is_on_remote(root, state.head, state.remote_name):
            raise gitutil.GitError(f"HEAD {state.head[:12]} is not on remote '{state.remote_name}'; push first "
                                   "(git push) or drop --pushed-only")
        excluded = []
        if state.tracked_dirty:
            changed = gitutil.git_text(["diff", "--name-only", "HEAD"], root).splitlines()
            excluded.append(f"{len(changed)} modified tracked file(s): {', '.join(changed[:8])}{' …' if len(changed) > 8 else ''}")
        if state.untracked:
            excluded.append(f"{len(state.untracked)} untracked file(s): {', '.join(state.untracked[:8])}{' …' if len(state.untracked) > 8 else ''}")
        if excluded:
            warnings.append("NOT included (pushed-only mode): " + "; ".join(excluded) +
                            f". The task is based on commit {state.head[:12]} exactly as it exists on the remote.")
        opts = SubmitOptions(**{**opts.__dict__, "include_commits": "no", "include_untracked": False})

    # 1. Uncommitted tracked changes -----------------------------------------
    patch = gitutil.uncommitted_patch(root) if (state.tracked_dirty and not opts.pushed_only) else b""
    if patch:
        changed = gitutil.git_text(["diff", "--name-only", "HEAD"], root).splitlines()
        warnings.append(f"including uncommitted changes to {len(changed)} tracked file(s) inside the bundle "
                        "(as a diff; no git credentials are involved). Use --pushed-only to exclude them.")
    patch_sha = gitutil.sha256_bytes(patch) if patch else None
    if patch:
        entries["uncommitted.patch"] = patch

    # 2. Untracked (not ignored) files ----------------------------------------
    untracked: list[str] = []
    if opts.include_untracked and state.untracked:
        if len(state.untracked) > MAX_UNTRACKED_FILES:
            warnings.append(f"{len(state.untracked)} untracked files; skipping them all (limit {MAX_UNTRACKED_FILES}). "
                            "Commit or ignore them, or use --no-untracked.")
        else:
            for rel in state.untracked:
                p = root / rel
                if not p.is_file():
                    continue
                if p.stat().st_size > MAX_UNTRACKED_FILE_BYTES:
                    warnings.append(f"skipping large untracked file {rel}")
                    continue
                entries[f"files/untracked/{rel}"] = p
                untracked.append(rel)

    # 3. Commits the worker cannot fetch from the remote ----------------------
    has_commit_bundle = False
    on_remote = gitutil.commit_is_on_remote(root, state.head, state.remote_name)
    want_bundle = opts.include_commits == "yes" or (opts.include_commits == "auto" and not on_remote)
    if want_bundle:
        tmp = state_dir() / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        bundle_file = tmp / f"{os.getpid()}-commits.bundle"
        gitutil.create_commit_bundle(root, state.head, state.remote_name, bundle_file)
        entries["commits.bundle"] = bundle_file.read_bytes()
        bundle_file.unlink(missing_ok=True)
        has_commit_bundle = True
        if not on_remote:
            warnings.append("base commit is not on the remote; local commits are shipped inside the task bundle")
    if not state.remote_url and not has_commit_bundle:
        raise gitutil.GitError("repository has no remote and no commit bundle was created; use --include-commits yes")

    # 4. Conversation context ---------------------------------------------------
    digest = None
    transcript_path: Path | None = None
    if opts.session != "none":
        if opts.session not in ("auto", "") and opts.session.endswith(".jsonl"):
            transcript_path = Path(opts.session).expanduser()
        else:
            transcript_path = ctx.find_transcript(cwd, None if opts.session == "auto" else opts.session)
        if transcript_path and transcript_path.is_file():
            digest = ctx.load_transcript(transcript_path)
        elif opts.session != "auto":
            warnings.append(f"transcript not found for session {opts.session!r}")
    transcript_member = None
    if opts.include_transcript and transcript_path and transcript_path.is_file():
        transcript_member = "transcript.jsonl"
        entries[transcript_member] = transcript_path

    # 5. Attached files ---------------------------------------------------------
    attached: list[str] = []
    for a in opts.attach:
        p = Path(a).expanduser()
        if not p.is_file():
            warnings.append(f"attachment not found: {a}")
            continue
        try:
            rel = str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            rel = p.name
        entries[f"files/attached/{rel}"] = p
        attached.append(rel)

    # 6. Manifest, brief, prompt -------------------------------------------------
    repo = RepoRef(remote_url=state.remote_url, branch=state.branch, base_commit=state.head,
                   has_uncommitted_patch=bool(patch), uncommitted_patch_sha256=patch_sha,
                   untracked_files=untracked, has_commit_bundle=has_commit_bundle)
    acceptance = Acceptance(tests=list(opts.tests), criteria=list(opts.criteria), timeout_seconds=opts.test_timeout_seconds)
    manifest = TaskManifest(
        task_id=new_task_id(), title=opts.title or _title_from_request(opts.request), request=opts.request,
        repo=repo, acceptance=acceptance, constraints=list(opts.constraints),
        context=ContextRef(brief_file="context.md", transcript_file=transcript_member, attached_files=attached,
                           source_session_id=digest.session_id if digest else None),
        agent=opts.agent, limits=opts.limits, created_by=socket.gethostname(),
    )
    brief = ctx.render_brief(request=opts.request, repo=repo.__dict__, digest=digest, notes=opts.notes,
                             criteria=opts.criteria, tests=opts.tests, constraints=opts.constraints,
                             attached_files=attached, max_chars=opts.max_context_chars)
    prompt = ctx.render_prompt(request=opts.request, tests=opts.tests, criteria=opts.criteria)
    entries["task.json"] = json.dumps(manifest.to_dict(), indent=2).encode()
    entries["context.md"] = brief.encode()
    entries["prompt.md"] = prompt.encode()
    return BuiltTask(manifest=manifest, bundle=bundlemod.pack(entries), brief=brief, prompt=prompt, warnings=warnings)


def record_submission(built: BuiltTask, repo_root: Path, coordinator_url: str) -> Path:
    """Remember locally what was submitted so ``apply`` can sanity-check later."""
    d = state_dir() / "submitted" / built.manifest.task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(built.manifest.to_dict(), indent=2))
    (d / "submission.json").write_text(json.dumps({"repo_root": str(repo_root), "coordinator_url": coordinator_url,
                                                   "submitted_at": built.manifest.created_at}, indent=2))
    (d / "context.md").write_text(built.brief)
    return d


def submit_task(transport: Transport, sealer: Sealer, built: BuiltTask) -> Any:
    meta = built.manifest.summary_meta()
    meta["sealed"] = sealer.name
    return transport.submit(built.manifest.task_id, meta, sealer.seal(built.bundle))
