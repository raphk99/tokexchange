"""Full pipeline with a fake agent: laptop A -> coordinator -> laptop B -> coordinator -> laptop A."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tokexchange import cli
from tokexchange.agent.fake import FakeAgent
from tokexchange.coordinator.server import CoordinatorServer, TokenStore
from tokexchange.crypto import sealer_for
from tokexchange.models import OUTCOME_NO_CHANGES, OUTCOME_SUCCESS, OUTCOME_TESTS_FAILED, OUTCOME_WORKSPACE_ERROR
from tokexchange.results import apply_result, fetch_result
from tokexchange.submit import SubmitOptions, build_task, record_submission, submit_task
from tokexchange.transport import open_transport
from tokexchange.worker import Worker, WorkerOptions
from tests.helpers import IsolatedEnv, git, make_origin_and_clone


def _has_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


class EndToEndTests(IsolatedEnv):
    def setUp(self):
        super().setUp()
        self.origin, self.repo_a = make_origin_and_clone(self.tmp)
        tokens = self.tmp / "tokens.json"
        self.tok_a = TokenStore.add_token(tokens, "laptop-a", ["submit"])
        self.tok_b = TokenStore.add_token(tokens, "laptop-b", ["work"])
        self.server = CoordinatorServer(self.tmp / "coord", tokens, port=0)
        self.server.start_background()
        self.secret = "shared-secret" if _has_crypto() else None
        self.sealer = sealer_for(self.secret)
        self.ta = open_transport(self.server.url, self.tok_a)
        self.tb = open_transport(self.server.url, self.tok_b)
        self.worker = Worker(self.tb, self.sealer, FakeAgent(),
                             WorkerOptions(worker_id="laptop-b", work_root=self.tmp / "work-b", heartbeat_interval=0.2))

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def _dirty_a(self):
        (self.repo_a / "app.py").write_text("def add(a, b):\n    return a + b  # edited on A\n")
        (self.repo_a / "notes.txt").write_text("untracked on A\n")

    def test_pipeline_with_uncommitted_changes_and_tests(self):
        self._dirty_a()
        request = ("Add a multiply function.\n"
                   "FAKE: write app.py <<def add(a, b):\\n    return a + b  # edited on A\\n\\ndef mul(a, b):\\n    return a * b\\n>>\n"
                   "FAKE: write tests_ok.sh <<#!/bin/sh\\ngrep -q mul app.py>>")
        built = build_task(self.repo_a, SubmitOptions(request=request, tests=["sh tests_ok.sh"], criteria=["mul exists"],
                                                      notes=["keep it simple"], session="none"))
        self.assertTrue(built.manifest.repo.has_uncommitted_patch)
        self.assertEqual(built.manifest.repo.untracked_files, ["notes.txt"])
        self.assertFalse(built.manifest.repo.has_commit_bundle)  # base commit is on origin
        self.assertIn("keep it simple", built.brief)
        record_submission(built, self.repo_a, self.server.url)
        rec = submit_task(self.ta, self.sealer, built)
        self.assertEqual(rec.meta["sealed"], self.sealer.name)
        if self.secret:
            self.assertTrue(self.server.store.bundle(rec.task_id).startswith(b"TXE1"))  # coordinator sees ciphertext only

        done = self.worker.run_once()
        self.assertEqual(done.status, "succeeded")
        out_dir, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_SUCCESS, report)
        self.assertNotEqual(report.baseline_commit, report.base_commit)
        self.assertEqual(sorted(report.patch["changed_paths"]), ["app.py", "tests_ok.sh"])
        self.assertEqual(report.tests[0]["exit_code"], 0)
        self.assertTrue((out_dir / "full.patch").is_file())
        full = (out_dir / "full.patch").read_text()
        self.assertIn("notes.txt", full)  # full patch includes A's baseline; changes.patch does not
        self.assertNotIn("notes.txt", (out_dir / "changes.patch").read_text())

        # Apply on A: uncommitted work preserved, new work added, nothing staged.
        outcome = apply_result(self.repo_a, out_dir)
        self.assertTrue(outcome.applied, outcome.error)
        self.assertEqual(outcome.warnings, [])
        self.assertIn("def mul", (self.repo_a / "app.py").read_text())
        self.assertIn("edited on A", (self.repo_a / "app.py").read_text())
        self.assertEqual((self.repo_a / "notes.txt").read_text(), "untracked on A\n")
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=self.repo_a), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.repo_a), report.base_commit)
        # workspace cleaned up on B, logs kept
        self.assertFalse((self.tmp / "work-b" / "tasks" / rec.task_id / "repo").exists())
        self.assertTrue((self.tmp / "work-b" / "tasks" / rec.task_id / "report.json").is_file())

    def test_local_commits_travel_in_bundle(self):
        (self.repo_a / "local.txt").write_text("committed locally, not pushed\n")
        git("add", "-A", cwd=self.repo_a)
        git("commit", "-q", "-m", "local only", cwd=self.repo_a)
        request = "FAKE: write local.txt <<committed locally, not pushed\\nplus worker edit\\n>>"
        built = build_task(self.repo_a, SubmitOptions(request=request, session="none"))
        self.assertTrue(built.manifest.repo.has_commit_bundle)
        self.assertTrue(any("not on the remote" in w for w in built.warnings))
        rec = submit_task(self.ta, self.sealer, built)
        self.worker.run_once()
        out_dir, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_SUCCESS, report)
        self.assertEqual(report.baseline_commit, report.base_commit)  # nothing uncommitted -> no baseline commit
        self.assertTrue(apply_result(self.repo_a, out_dir).applied)
        self.assertIn("plus worker edit", (self.repo_a / "local.txt").read_text())

    def test_failing_tests_still_return_patch(self):
        request = "FAKE: write x.txt <<hello>>"
        built = build_task(self.repo_a, SubmitOptions(request=request, tests=["test -f does-not-exist"], session="none"))
        rec = submit_task(self.ta, self.sealer, built)
        self.assertEqual(self.worker.run_once().status, "succeeded")
        _, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_TESTS_FAILED)
        self.assertEqual(report.tests[0]["exit_code"], 1)
        self.assertIsNotNone(report.patch)

    def test_build_junk_is_excluded_from_patch(self):
        request = "FAKE: write __pycache__/x.cpython-314.pyc <<junk>>\nFAKE: write real.txt <<kept>>"
        built = build_task(self.repo_a, SubmitOptions(request=request, session="none"))
        rec = submit_task(self.ta, self.sealer, built)
        self.worker.run_once()
        _, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_SUCCESS, report)
        self.assertEqual(report.patch["changed_paths"], ["real.txt"])

    def test_no_changes_outcome(self):
        built = build_task(self.repo_a, SubmitOptions(request="FAKE: nothing", session="none"))
        rec = submit_task(self.ta, self.sealer, built)
        self.worker.run_once()
        _, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_NO_CHANGES)
        self.assertIsNone(report.patch)

    def test_unreachable_base_commit_is_reported(self):
        (self.repo_a / "z.txt").write_text("z")
        git("add", "-A", cwd=self.repo_a)
        git("commit", "-q", "-m", "unpushed", cwd=self.repo_a)
        built = build_task(self.repo_a, SubmitOptions(request="FAKE: nothing", session="none", include_commits="no"))
        rec = submit_task(self.ta, self.sealer, built)
        self.assertEqual(self.worker.run_once().status, "failed")
        _, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_WORKSPACE_ERROR)
        self.assertIn("not reachable", report.error)

    def test_pushed_only_excludes_local_work_with_warning(self):
        self._dirty_a()
        built = build_task(self.repo_a, SubmitOptions(request="FAKE: write y.txt <<y>>", session="none", pushed_only=True))
        self.assertFalse(built.manifest.repo.has_uncommitted_patch)
        self.assertEqual(built.manifest.repo.untracked_files, [])
        self.assertFalse(built.manifest.repo.has_commit_bundle)
        self.assertTrue(any("NOT included" in w and "app.py" in w and "notes.txt" in w for w in built.warnings), built.warnings)
        self.assertNotIn("baseline", built.brief)
        rec = submit_task(self.ta, self.sealer, built)
        self.worker.run_once()
        out_dir, report = fetch_result(self.ta, self.sealer, rec.task_id)
        self.assertEqual(report.outcome, OUTCOME_SUCCESS, report)
        self.assertEqual(report.baseline_commit, report.base_commit)
        self.assertFalse((out_dir / "full.patch").exists())
        # applies onto A even though A still has its own uncommitted work
        self.assertTrue(apply_result(self.repo_a, out_dir).applied)
        self.assertIn("edited on A", (self.repo_a / "app.py").read_text())

    def test_pushed_only_refuses_unpushed_head(self):
        (self.repo_a / "z.txt").write_text("z")
        git("add", "-A", cwd=self.repo_a)
        git("commit", "-q", "-m", "unpushed", cwd=self.repo_a)
        from tokexchange.gitutil import GitError
        with self.assertRaises(GitError) as ctx:
            build_task(self.repo_a, SubmitOptions(request="x", session="none", pushed_only=True))
        self.assertIn("push first", str(ctx.exception))

    def test_default_mode_announces_uncommitted_inclusion(self):
        self._dirty_a()
        built = build_task(self.repo_a, SubmitOptions(request="FAKE: nothing", session="none"))
        self.assertTrue(any("including uncommitted changes" in w for w in built.warnings))

    def test_transcript_digest_lands_in_brief(self):
        from tokexchange import context as ctx
        tdir = ctx.project_transcript_dir(self.repo_a)
        tdir.mkdir(parents=True)
        (tdir / "abc.jsonl").write_text(json.dumps({"type": "user", "sessionId": "abc", "origin": {"kind": "human"},
                                                    "message": {"role": "user", "content": "We decided on SQLite."}}) + "\n")
        built = build_task(self.repo_a, SubmitOptions(request="FAKE: nothing", session="auto"))
        self.assertIn("We decided on SQLite.", built.brief)
        self.assertEqual(built.manifest.context.source_session_id, "abc")

    def test_cli_dry_run_and_apply_check(self):
        self._dirty_a()
        out = self.tmp / "dry"
        code = cli.main(["submit", "FAKE: nothing", "--session", "none", "--dry-run", str(out), "--test", "true"]) if False else None
        # run through the CLI from inside repo A
        import os
        cwd = os.getcwd()
        os.chdir(self.repo_a)
        try:
            code = cli.main(["submit", "FAKE: nothing", "--session", "none", "--dry-run", str(out), "--test", "true"])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertTrue((out / "context.md").is_file())
        self.assertTrue((out / "prompt.md").is_file())
        task = json.loads((out / "task.json").read_text())
        self.assertEqual(task["acceptance"]["tests"], ["true"])
