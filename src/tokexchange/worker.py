"""Worker side (Laptop B): claim tasks, prepare an isolated workspace, run the agent, ship a patch."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bundle as bundlemod
from . import gitutil
from .agent import Agent
from .crypto import Sealer
from .models import (OUTCOME_AGENT_ERROR, OUTCOME_NO_CHANGES, OUTCOME_SUCCESS, OUTCOME_TESTS_FAILED, OUTCOME_TIMEOUT,
                     OUTCOME_WORKSPACE_ERROR, STATUS_CANCELLED, STATUS_FAILED, STATUS_RUNNING, STATUS_SUCCEEDED,
                     ExecutionReport, TaskManifest, now_iso)
from .transport import Transport, TransportError
from .transport.base import TaskRecord

log = logging.getLogger("tokexchange.worker")


class WorkspaceError(RuntimeError):
    pass


@dataclass
class WorkerOptions:
    worker_id: str
    work_root: Path
    poll_interval: float = 15.0
    concurrency: int = 1
    keep_workspaces: bool = False
    heartbeat_interval: float = 60.0


class Worker:
    def __init__(self, transport: Transport, sealer: Sealer, agent: Agent, opts: WorkerOptions):
        self.transport, self.sealer, self.agent, self.opts = transport, sealer, agent, opts
        self.opts.work_root = Path(self.opts.work_root).expanduser()
        self.opts.work_root.mkdir(parents=True, exist_ok=True)
        self._mirror_locks: dict[str, threading.Lock] = {}
        self._mirror_locks_guard = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ loop
    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> TaskRecord | None:
        rec = self.transport.claim(self.opts.worker_id)
        if rec is None:
            return None
        log.info("claimed task %s (%s)", rec.task_id, rec.meta.get("title"))
        return self.execute(rec)

    def run_forever(self) -> None:
        problems = self.agent.preflight()
        if problems:
            raise WorkspaceError("agent preflight failed: " + "; ".join(problems))
        with ThreadPoolExecutor(max_workers=self.opts.concurrency) as pool:
            in_flight: set = set()
            while not self._stop.is_set():
                in_flight = {f for f in in_flight if not f.done()}
                if len(in_flight) < self.opts.concurrency:
                    try:
                        rec = self.transport.claim(self.opts.worker_id)
                    except TransportError as exc:
                        log.warning("claim failed: %s", exc)
                        rec = None
                    if rec is not None:
                        log.info("claimed task %s (%s)", rec.task_id, rec.meta.get("title"))
                        in_flight.add(pool.submit(self._execute_logged, rec))
                        continue
                self._stop.wait(self.opts.poll_interval)

    def _execute_logged(self, rec: TaskRecord) -> TaskRecord:
        try:
            return self.execute(rec)
        except Exception:  # pragma: no cover - last resort
            log.error("task %s crashed:\n%s", rec.task_id, traceback.format_exc())
            raise

    # --------------------------------------------------------------- helpers
    def _mirror_lock(self, key: str) -> threading.Lock:
        with self._mirror_locks_guard:
            return self._mirror_locks.setdefault(key, threading.Lock())

    def _mirror_path(self, remote_url: str | None) -> Path:
        key = hashlib.sha1((remote_url or "local").encode()).hexdigest()[:16]
        return self.opts.work_root / "mirrors" / f"{key}.git"

    def _prepare_workspace(self, manifest: TaskManifest, bundle_dir: Path, task_dir: Path) -> tuple[Path, str]:
        repo = manifest.repo
        mirror = self._mirror_path(repo.remote_url)
        with self._mirror_lock(str(mirror)):
            if repo.remote_url:
                try:
                    gitutil.ensure_mirror(repo.remote_url, mirror)
                except gitutil.GitError as exc:
                    if not repo.has_commit_bundle or not (mirror / "HEAD").exists():
                        raise WorkspaceError(f"cannot clone/fetch {repo.remote_url}: {exc}") from exc
                    log.warning("remote fetch failed, relying on commit bundle: %s", exc)
            elif not (mirror / "HEAD").exists():
                mirror.parent.mkdir(parents=True, exist_ok=True)
                gitutil.run_git(["init", "--bare", "-q", str(mirror)], mirror.parent)
            if repo.has_commit_bundle:
                gitutil.fetch_bundle_into(mirror, bundle_dir / "commits.bundle", repo.commit_bundle_ref)
            if not gitutil.commit_exists(mirror, repo.base_commit):
                raise WorkspaceError(f"base commit {repo.base_commit} is not reachable: push it or resubmit with --include-commits yes")
            worktree = task_dir / "repo"
            if worktree.exists():
                gitutil.remove_worktree(mirror, worktree)
                shutil.rmtree(worktree, ignore_errors=True)
            gitutil.add_worktree(mirror, worktree, repo.base_commit)
        # Keep helper files out of the diff.
        exclude = Path(gitutil.git_text(["rev-parse", "--git-path", "info/exclude"], worktree))
        if not exclude.is_absolute():
            exclude = worktree / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as fh:
            fh.write("\n.tokexchange/\n")
        # Baseline: the submitter's uncommitted work.
        baseline = repo.base_commit
        patch_file = bundle_dir / "uncommitted.patch"
        applied_anything = False
        if repo.has_uncommitted_patch and patch_file.is_file():
            gitutil.apply_patch(worktree, patch_file.read_bytes(), index=True)
            applied_anything = True
        untracked_root = bundle_dir / "files" / "untracked"
        if untracked_root.is_dir():
            for src in untracked_root.rglob("*"):
                if src.is_file():
                    dst = worktree / src.relative_to(untracked_root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    applied_anything = True
        if applied_anything:
            baseline = gitutil.commit_all(worktree, "tokexchange: submitter's uncommitted baseline", allow_empty=False)
        attached_root = bundle_dir / "files" / "attached"
        if attached_root.is_dir():
            shutil.copytree(attached_root, worktree / ".tokexchange" / "attached", dirs_exist_ok=True)
        return worktree, baseline

    def _run_tests(self, manifest: TaskManifest, worktree: Path, log_dir: Path) -> list[dict[str, Any]]:
        results = []
        timeout = int(manifest.acceptance.timeout_seconds or 900)
        for i, cmd in enumerate(manifest.acceptance.tests):
            log_file = log_dir / f"test_{i}.log"
            start = time.monotonic()
            timed_out = False
            with log_file.open("wb") as fh:
                fh.write(f"$ {cmd}\n".encode())
                fh.flush()
                try:
                    proc = subprocess.run(cmd, shell=True, cwd=str(worktree), stdout=fh, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, timeout=timeout)
                    code: int | None = proc.returncode
                except subprocess.TimeoutExpired:
                    code, timed_out = None, True
                    fh.write(b"\n[tokexchange] test command timed out\n")
            results.append({"command": cmd, "exit_code": code, "duration_seconds": round(time.monotonic() - start, 2),
                            "log_file": log_file.name, "timed_out": timed_out})
        return results

    # --------------------------------------------------------------- execute
    def execute(self, rec: TaskRecord) -> TaskRecord:
        task_id = rec.task_id
        task_dir = self.opts.work_root / "tasks" / task_id
        bundle_dir, log_dir = task_dir / "bundle", task_dir / "logs"
        started = now_iso()
        cancel = threading.Event()
        stop_hb = threading.Event()

        def heartbeat() -> None:
            while not stop_hb.wait(self.opts.heartbeat_interval):
                try:
                    r = self.transport.heartbeat(task_id, self.opts.worker_id)
                    if r.status == STATUS_CANCELLED:
                        cancel.set()
                        return
                except TransportError as exc:
                    log.warning("heartbeat failed for %s: %s", task_id, exc)

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        report = ExecutionReport(task_id=task_id, outcome=OUTCOME_WORKSPACE_ERROR, worker_id=self.opts.worker_id,
                                 started_at=started, finished_at=started, base_commit=rec.meta.get("base_commit", ""))
        entries: dict[str, bytes | Path] = {}
        mirror: Path | None = None
        worktree: Path | None = None
        try:
            self.transport.heartbeat(task_id, self.opts.worker_id, status=STATUS_RUNNING, message="downloading bundle")
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)
            bundle_dir.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            raw = self.sealer.open(self.transport.get_bundle(task_id))
            bundlemod.unpack(raw, bundle_dir)
            raw_manifest = json.loads((bundle_dir / "task.json").read_text())
            manifest = TaskManifest.from_dict(raw_manifest)
            report.base_commit = manifest.repo.base_commit
            mirror = self._mirror_path(manifest.repo.remote_url)

            self.transport.heartbeat(task_id, self.opts.worker_id, message="preparing workspace")
            worktree, baseline = self._prepare_workspace(manifest, bundle_dir, task_dir)
            report.baseline_commit = baseline

            brief_file = bundle_dir / manifest.context.brief_file
            prompt = (bundle_dir / "prompt.md").read_text() if (bundle_dir / "prompt.md").is_file() else manifest.request
            transcript = bundle_dir / manifest.context.transcript_file if manifest.context.transcript_file else None
            self.transport.heartbeat(task_id, self.opts.worker_id, message="running agent")
            agent_result = self.agent.run(workspace=worktree, prompt=prompt, brief_file=brief_file, spec=manifest.agent,
                                          log_dir=log_dir, transcript_file=transcript, test_commands=manifest.acceptance.tests,
                                          cancel_event=cancel)
            report.agent = agent_result.to_dict()
            for name in ("agent_stdout.json", "agent_stderr.log", "agent_result.json", "agent_command.txt"):
                if (log_dir / name).is_file():
                    entries[f"logs/{name}"] = log_dir / name

            if cancel.is_set():
                report.outcome = OUTCOME_AGENT_ERROR
                report.error = "cancelled by submitter"
                return rec

            # Patch generation ------------------------------------------------
            changes = gitutil.diff_against(worktree, baseline)
            files_changed, ins, dels, paths = gitutil.diff_stats(worktree, baseline)
            if changes.strip():
                entries["changes.patch"] = changes
                report.patch = {"file": "changes.patch", "files_changed": files_changed, "insertions": ins, "deletions": dels,
                                "sha256": gitutil.sha256_bytes(changes), "changed_paths": paths, "full_patch_file": None}
                if baseline != manifest.repo.base_commit:
                    full = gitutil.diff_against(worktree, manifest.repo.base_commit)
                    entries["full.patch"] = full
                    report.patch["full_patch_file"] = "full.patch"

            # Tests -----------------------------------------------------------
            if manifest.acceptance.tests and changes.strip():
                self.transport.heartbeat(task_id, self.opts.worker_id, message="running tests")
                report.tests = self._run_tests(manifest, worktree, log_dir)
                for t in report.tests:
                    entries[f"logs/{t['log_file']}"] = log_dir / t["log_file"]

            # Outcome -----------------------------------------------------------
            if agent_result.timed_out:
                report.outcome = OUTCOME_TIMEOUT
            elif agent_result.is_error:
                report.outcome = OUTCOME_AGENT_ERROR
                report.error = agent_result.summary[:2000]
            elif not changes.strip():
                report.outcome = OUTCOME_NO_CHANGES
            elif any(t["exit_code"] != 0 for t in report.tests):
                report.outcome = OUTCOME_TESTS_FAILED
            else:
                report.outcome = OUTCOME_SUCCESS
        except (WorkspaceError, gitutil.GitError, bundlemod.BundleError, ValueError, OSError) as exc:
            report.outcome = OUTCOME_WORKSPACE_ERROR
            report.error = f"{type(exc).__name__}: {exc}"
            log.error("task %s failed: %s", task_id, report.error)
        finally:
            stop_hb.set()
            report.finished_at = now_iso()
            entries["report.json"] = json.dumps(report.to_dict(), indent=2).encode()
            final_status = STATUS_SUCCEEDED if report.outcome in (OUTCOME_SUCCESS, OUTCOME_TESTS_FAILED, OUTCOME_NO_CHANGES) else STATUS_FAILED
            result_meta = {"outcome": report.outcome, "error": report.error, "patch": report.patch and
                           {k: report.patch[k] for k in ("files_changed", "insertions", "deletions")},
                           "tests_passed": all(t["exit_code"] == 0 for t in report.tests) if report.tests else None,
                           "agent": {k: (report.agent or {}).get(k) for k in ("num_turns", "cost_usd_estimate", "session_id")}}
            if not cancel.is_set():
                try:
                    rec = self.transport.upload_result(task_id, self.opts.worker_id, result_meta,
                                                       self.sealer.seal(bundlemod.pack(entries)), final_status)
                except TransportError as exc:
                    log.error("uploading result for %s failed: %s", task_id, exc)
            (task_dir / "report.json").write_text(json.dumps(report.to_dict(), indent=2))
            if worktree is not None and mirror is not None and not self.opts.keep_workspaces:
                gitutil.remove_worktree(mirror, worktree)
            log.info("task %s finished: %s", task_id, report.outcome)
        return rec
