"""Submitter side (Laptop A): download a result bundle and apply the patch."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import bundle as bundlemod
from . import gitutil
from .config import state_dir
from .crypto import Sealer
from .models import ExecutionReport
from .transport import Transport


def fetch_result(transport: Transport, sealer: Sealer, task_id: str, out_dir: Path | None = None) -> tuple[Path, ExecutionReport]:
    out_dir = out_dir or (state_dir() / "results" / task_id)
    data = sealer.open(transport.get_result(task_id))
    bundlemod.unpack(data, out_dir)
    report = ExecutionReport.from_dict(json.loads((out_dir / "report.json").read_text()))
    return out_dir, report


@dataclass
class ApplyOutcome:
    applied: bool
    patch_file: Path | None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def apply_result(repo_path: Path, result_dir: Path, *, check_only: bool = False, three_way: bool = False,
                 which: str = "changes") -> ApplyOutcome:
    """Apply ``changes.patch`` (diff vs. the baseline that included A's uncommitted work).

    Only the working tree is touched; the index and existing uncommitted changes are left alone.
    """
    warnings: list[str] = []
    report = ExecutionReport.from_dict(json.loads((result_dir / "report.json").read_text()))
    patch_name = "changes.patch" if which == "changes" else "full.patch"
    patch_file = result_dir / patch_name
    if not patch_file.is_file():
        return ApplyOutcome(False, None, warnings, f"result contains no {patch_name} (outcome: {report.outcome})")
    patch = patch_file.read_bytes()
    if not patch.strip():
        return ApplyOutcome(False, patch_file, warnings, "patch is empty")
    root = gitutil.repo_root(repo_path)
    head = gitutil.git_text(["rev-parse", "HEAD"], root)
    if head != report.base_commit:
        warnings.append(f"HEAD ({head[:12]}) differs from the task's base commit ({report.base_commit[:12]}); expect conflicts")
    submitted = state_dir() / "submitted" / report.task_id / "manifest.json"
    if submitted.is_file():
        manifest = json.loads(submitted.read_text())
        expected = manifest["repo"].get("uncommitted_patch_sha256")
        current = gitutil.uncommitted_patch(root)
        current_sha = gitutil.sha256_bytes(current) if current.strip() else None
        if expected != current_sha:
            warnings.append("your uncommitted changes differ from what was submitted; the patch was made on top of the submitted state")
    ok, msg = gitutil.check_patch(root, patch)
    if not ok and not three_way:
        return ApplyOutcome(False, patch_file, warnings, f"patch does not apply cleanly:\n{msg.strip()}\nTry --3way, or apply manually: git apply {patch_file}")
    if check_only:
        return ApplyOutcome(False, patch_file, warnings, None)
    try:
        gitutil.apply_patch(root, patch, index=False, three_way=three_way)
    except gitutil.GitError as exc:
        return ApplyOutcome(False, patch_file, warnings, str(exc))
    return ApplyOutcome(True, patch_file, warnings, None)
