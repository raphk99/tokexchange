import unittest

from tokexchange.models import AgentSpec, ExecutionReport, RepoRef, TaskManifest


class ModelTests(unittest.TestCase):
    def test_manifest_roundtrip(self):
        m = TaskManifest(task_id="t1", title="x", request="do it", repo=RepoRef(remote_url="u", branch="main", base_commit="abc"),
                         agent=AgentSpec(max_turns=3))
        d = m.to_dict()
        back = TaskManifest.from_dict(d)
        self.assertEqual(back.agent.max_turns, 3)
        self.assertEqual(back.repo.base_commit, "abc")
        self.assertEqual(back.summary_meta()["base_commit"], "abc")
        self.assertNotIn("request", back.summary_meta())

    def test_newer_schema_rejected(self):
        with self.assertRaises(ValueError):
            TaskManifest.from_dict({"schema_version": 99, "task_id": "t", "title": "t", "request": "r",
                                    "repo": {"remote_url": None, "branch": "b", "base_commit": "c"}})

    def test_report_roundtrip_ignores_unknown(self):
        r = ExecutionReport.from_dict({"task_id": "t", "outcome": "success", "worker_id": "w", "started_at": "a",
                                       "finished_at": "b", "base_commit": "c", "unknown_future_field": 1})
        self.assertTrue(r.ok)
