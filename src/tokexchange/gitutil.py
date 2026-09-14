"""Thin wrappers around the ``git`` CLI used on both laptops.

Nothing here mutates the *submitting* repository's working tree or index.
Functions that create or modify repositories are only used inside worker
workspaces.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path | str, check: bool = True, input_bytes: bytes | None = None,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    # Never run repository hooks from code we did not author; keep output stable.
    full_env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    if env:
        full_env.update(env)
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, input=input_bytes, env=full_env)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}")
    return proc


def git_text(args: list[str], cwd: Path | str) -> str:
    return run_git(args, cwd).stdout.decode(errors="replace").strip()


def is_git_repo(path: Path | str) -> bool:
    proc = run_git(["rev-parse", "--is-inside-work-tree"], path, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == b"true"


def repo_root(path: Path | str) -> Path:
    return Path(git_text(["rev-parse", "--show-toplevel"], path))


@dataclass
class RepoState:
    root: Path
    branch: str
    head: str
    remote_url: str | None
    remote_name: str | None
    tracked_dirty: bool
    untracked: list[str]


def inspect_repo(path: Path | str) -> RepoState:
    root = repo_root(path)
    head = git_text(["rev-parse", "HEAD"], root)
    branch = git_text(["rev-parse", "--abbrev-ref", "HEAD"], root)
    remote_name = None
    remote_url = None
    if branch != "HEAD":
        proc = run_git(["config", f"branch.{branch}.remote"], root, check=False)
        if proc.returncode == 0:
            remote_name = proc.stdout.decode().strip()
    if remote_name is None:
        remotes = git_text(["remote"], root).split()
        if "origin" in remotes:
            remote_name = "origin"
        elif remotes:
            remote_name = remotes[0]
    if remote_name:
        proc = run_git(["remote", "get-url", remote_name], root, check=False)
        if proc.returncode == 0:
            remote_url = proc.stdout.decode().strip()
    tracked_dirty = run_git(["diff", "--quiet", "HEAD"], root, check=False).returncode != 0
    untracked_out = git_text(["ls-files", "--others", "--exclude-standard", "-z"], root)
    untracked = [p for p in untracked_out.split("\0") if p]
    return RepoState(root=root, branch=branch, head=head, remote_url=remote_url, remote_name=remote_name,
                     tracked_dirty=tracked_dirty, untracked=untracked)


def uncommitted_patch(root: Path) -> bytes:
    """Diff of tracked files (staged + unstaged) against HEAD, binary-safe."""
    return run_git(["diff", "--binary", "--no-color", "HEAD"], root).stdout


def commit_is_on_remote(root: Path, commit: str, remote_name: str | None) -> bool:
    """True if ``commit`` is reachable from any remote-tracking ref of ``remote_name``."""
    if not remote_name:
        return False
    proc = run_git(["branch", "-r", "--contains", commit, "--format=%(refname)"], root, check=False)
    if proc.returncode != 0:
        return False
    prefix = f"refs/remotes/{remote_name}/"
    return any(line.strip().startswith(prefix) for line in proc.stdout.decode().splitlines())


def create_commit_bundle(root: Path, commit: str, remote_name: str | None, out_file: Path,
                         ref_name: str = "refs/tokexchange/base") -> None:
    """Write a git bundle containing ``commit`` and ancestors not already on the remote.

    The bundle carries a single ref (``ref_name``) pointing at ``commit`` so the
    worker can fetch it by name regardless of branch state on the submitter.
    """
    run_git(["update-ref", ref_name, commit], root)
    try:
        exclusions: list[str] = []
        if remote_name:
            refs = git_text(["for-each-ref", "--format=%(refname)", f"refs/remotes/{remote_name}/"], root)
            exclusions = [f"^{r}" for r in refs.split() if r]
        run_git(["bundle", "create", str(out_file), ref_name, *exclusions], root)
    finally:
        run_git(["update-ref", "-d", ref_name], root, check=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Worker-side helpers (operate only inside the worker's own workspace root).
# ---------------------------------------------------------------------------

def ensure_mirror(remote_url: str, mirror_path: Path) -> None:
    if (mirror_path / "HEAD").exists():
        run_git(["fetch", "--prune", "--tags", "origin", "+refs/heads/*:refs/heads/*"], mirror_path)
    else:
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        run_git(["clone", "--mirror", remote_url, str(mirror_path)], mirror_path.parent)


def fetch_bundle_into(mirror_path: Path, bundle_file: Path, ref_name: str) -> None:
    run_git(["fetch", str(bundle_file), f"{ref_name}:{ref_name}"], mirror_path)


def commit_exists(repo: Path, commit: str) -> bool:
    return run_git(["cat-file", "-e", f"{commit}^{{commit}}"], repo, check=False).returncode == 0


def add_worktree(mirror_path: Path, worktree: Path, commit: str) -> None:
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "--detach", str(worktree), commit], mirror_path)
    # Do not run hooks that came with the repository while the agent works.
    run_git(["config", "core.hooksPath", os.devnull], worktree)


def remove_worktree(mirror_path: Path, worktree: Path) -> None:
    run_git(["worktree", "remove", "--force", str(worktree)], mirror_path, check=False)
    run_git(["worktree", "prune"], mirror_path, check=False)


AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "tokexchange", "GIT_AUTHOR_EMAIL": "tokexchange@localhost",
    "GIT_COMMITTER_NAME": "tokexchange", "GIT_COMMITTER_EMAIL": "tokexchange@localhost",
}


def apply_patch(repo: Path, patch: bytes, index: bool = True, three_way: bool = False) -> None:
    if not patch.strip():
        return
    args = ["apply", "--binary", "--whitespace=nowarn"]
    if index:
        args.append("--index")
    if three_way:
        args.append("--3way")
    run_git(args, repo, input_bytes=patch)


def check_patch(repo: Path, patch: bytes) -> tuple[bool, str]:
    if not patch.strip():
        return True, ""
    proc = run_git(["apply", "--check", "--binary", "--whitespace=nowarn"], repo, check=False, input_bytes=patch)
    return proc.returncode == 0, proc.stderr.decode(errors="replace")


def commit_all(repo: Path, message: str, allow_empty: bool = True) -> str:
    run_git(["add", "-A"], repo)
    args = ["-c", "commit.gpgsign=false", "commit", "-q", "-m", message, "--no-verify"]
    if allow_empty:
        args.append("--allow-empty")
    run_git(args, repo, env=AUTHOR_ENV)
    return git_text(["rev-parse", "HEAD"], repo)


def diff_against(repo: Path, commit: str) -> bytes:
    """Stage everything (so new files are included) and diff the index against ``commit``."""
    run_git(["add", "-A"], repo)
    return run_git(["diff", "--cached", "--binary", "--no-color", commit], repo).stdout


def diff_stats(repo: Path, commit: str) -> tuple[int, int, int, list[str]]:
    numstat = git_text(["diff", "--cached", "--numstat", commit], repo)
    files, ins, dels, paths = 0, 0, 0, []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        ins += int(parts[0]) if parts[0].isdigit() else 0
        dels += int(parts[1]) if parts[1].isdigit() else 0
        paths.append(parts[2])
    return files, ins, dels, paths
