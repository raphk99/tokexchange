import datetime as dt

from tokexchange.models import STATUS_CLAIMED, STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED
from tokexchange.transport import TransportError, open_transport
from tokexchange.transport.local import LocalDirTransport
from tests.helpers import IsolatedEnv


class LocalTransportTests(IsolatedEnv):
    def test_lifecycle(self):
        t = open_transport(f"file://{self.tmp / 'q'}")
        self.assertIsInstance(t, LocalDirTransport)
        rec = t.submit("task-1", {"title": "one", "lease_seconds": 60, "max_attempts": 2}, b"bundle")
        self.assertEqual(rec.status, STATUS_QUEUED)
        with self.assertRaises(TransportError):
            t.submit("task-1", {}, b"dup")
        claimed = t.claim("wb")
        self.assertEqual(claimed.task_id, "task-1")
        self.assertEqual(claimed.status, STATUS_CLAIMED)
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNone(t.claim("wb2"))
        self.assertEqual(t.get_bundle("task-1"), b"bundle")
        with self.assertRaises(TransportError):
            t.heartbeat("task-1", "someone-else")
        rec = t.heartbeat("task-1", "wb", status=STATUS_RUNNING, message="working")
        self.assertEqual(rec.status, STATUS_RUNNING)
        with self.assertRaises(TransportError):
            t.get_result("task-1")
        rec = t.upload_result("task-1", "wb", {"outcome": "success"}, b"result", STATUS_SUCCEEDED)
        self.assertEqual(rec.status, STATUS_SUCCEEDED)
        self.assertEqual(t.get_result("task-1"), b"result")
        self.assertEqual([r.task_id for r in t.list()], ["task-1"])

    def test_lease_expiry_requeues_then_fails(self):
        t = LocalDirTransport(self.tmp / "q")
        t.submit("task-2", {"lease_seconds": 60, "max_attempts": 2}, b"b")
        rec = t.claim("w1")
        rec.lease_expires_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
        t._write(rec)
        rec = t.claim("w2")
        self.assertEqual(rec.worker_id, "w2")
        self.assertEqual(rec.attempts, 2)
        rec.lease_expires_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
        t._write(rec)
        self.assertIsNone(t.claim("w3"))
        self.assertEqual(t.get("task-2").status, "failed")

    def test_cancel(self):
        t = LocalDirTransport(self.tmp / "q")
        t.submit("task-3", {}, b"b")
        self.assertEqual(t.cancel("task-3").status, "cancelled")
        self.assertIsNone(t.claim("w"))
