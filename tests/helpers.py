"""Shared fixtures: temporary git repositories and environment isolation."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def make_origin_and_clone(base: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' with one commit and a working clone ('laptop A')."""
    seed = base / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("# demo\n")
    (seed / "app.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "initial", cwd=seed)
    origin = base / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(origin), cwd=base)
    clone = base / "laptop-a"
    git("clone", "-q", str(origin), str(clone), cwd=base)
    return origin, clone


class IsolatedEnv(unittest.TestCase):
    """Redirects tokexchange state, Claude config and HOME into a temp dir."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="tokexchange-test-"))
        self._env_backup = dict(os.environ)
        os.environ["TOKEXCHANGE_STATE_DIR"] = str(self.tmp / "state")
        os.environ["TOKEXCHANGE_CONFIG"] = str(self.tmp / "config.json")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("TOKEXCHANGE_URL", None)
        os.environ.pop("TOKEXCHANGE_TOKEN", None)
        os.environ.pop("TOKEXCHANGE_SECRET", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()
