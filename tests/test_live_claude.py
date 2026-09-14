"""Live test against the real ``claude`` binary and the machine's own login.

Skipped unless TOKEXCHANGE_LIVE=1. It costs a little of your usage quota.
"""
from __future__ import annotations

import os
import shutil
import unittest

from tokexchange.agent.claude_code import ClaudeCodeAgent
from tokexchange.models import OUTCOME_SUCCESS
from tokexchange.results import apply_result, fetch_result
from tokexchange.submit import SubmitOptions, build_task, submit_task
from tokexchange.crypto import NullSealer
from tokexchange.transport import open_transport
from tokexchange.worker import Worker, WorkerOptions
from tokexchange.models import AgentSpec
from tests.helpers import IsolatedEnv, make_origin_and_clone


@unittest.skipUnless(os.environ.get("TOKEXCHANGE_LIVE") == "1" and shutil.which("claude"), "set TOKEXCHANGE_LIVE=1 to run")
class LiveClaudeTests(IsolatedEnv):
    def setUp(self):
        super().setUp()
        # The live run must see the machine's real Claude login, not the isolated config dir.
        if "CLAUDE_CONFIG_DIR" in self._env_backup:
            os.environ["CLAUDE_CONFIG_DIR"] = self._env_backup["CLAUDE_CONFIG_DIR"]
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_real_claude_run(self):
        origin, repo_a = make_origin_and_clone(self.tmp)
        (repo_a / "app.py").write_text("def add(a, b):\n    return a + b\n\n# TODO: mul\n")
        t = open_transport(f"file://{self.tmp / 'queue'}")
        agent = ClaudeCodeAgent()
        self.assertEqual(agent.preflight(), [])
        opts = SubmitOptions(request="Add a `mul(a, b)` function to app.py that returns a * b, right after `add`. Keep the existing TODO comment removed. Do nothing else.",
                             tests=["python3 -c 'import app; assert app.mul(3,4)==12'"], session="none",
                             agent=AgentSpec(max_turns=8, timeout_seconds=300, model=os.environ.get("TOKEXCHANGE_LIVE_MODEL", "sonnet")))
        built = build_task(repo_a, opts)
        rec = submit_task(t, NullSealer(), built)
        worker = Worker(t, NullSealer(), agent, WorkerOptions(worker_id="live", work_root=self.tmp / "work"))
        worker.run_once()
        out_dir, report = fetch_result(t, NullSealer(), rec.task_id)
        print("\nLIVE REPORT:", report.outcome, report.error, report.agent and report.agent.get("summary"))
        self.assertEqual(report.outcome, OUTCOME_SUCCESS, report)
        self.assertTrue(apply_result(repo_a, out_dir).applied)
        self.assertIn("def mul", (repo_a / "app.py").read_text())
