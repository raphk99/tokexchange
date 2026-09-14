import json
import threading
import urllib.request
from pathlib import Path

from tokexchange.coordinator.server import CoordinatorServer, TokenStore
from tokexchange.models import STATUS_CANCELLED, STATUS_CLAIMED, STATUS_QUEUED, STATUS_SUCCEEDED
from tokexchange.transport import TransportError, open_transport
from tests.helpers import IsolatedEnv


class CoordinatorTests(IsolatedEnv):
    def setUp(self):
        super().setUp()
        self.tokens_file = self.tmp / "tokens.json"
        self.submit_token = TokenStore.add_token(self.tokens_file, "laptop-a", ["submit"])
        self.work_token = TokenStore.add_token(self.tokens_file, "laptop-b", ["work"])
        self.server = CoordinatorServer(self.tmp / "data", self.tokens_file, host="127.0.0.1", port=0)
        self.server.start_background()
        self.a = open_transport(self.server.url, self.submit_token)
        self.b = open_transport(self.server.url, self.work_token)

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def _raw(self, method, path, token=None, body=None):
        req = urllib.request.Request(self.server.url + path, method=method, data=body)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def test_auth_and_roles(self):
        self.assertEqual(self._raw("GET", "/v1/health")[0], 401)
        self.assertEqual(self._raw("GET", "/v1/health", "bogus")[0], 401)
        self.assertEqual(self._raw("GET", "/v1/health", self.submit_token)[0], 200)
        # a work-only token cannot submit; a submit-only token cannot claim
        self.assertEqual(self._raw("POST", "/v1/tasks/x1", self.work_token, b"")[0], 403)
        self.assertEqual(self._raw("POST", "/v1/tasks/claim", self.submit_token, b"{}")[0], 403)
        self.assertEqual(self._raw("GET", "/v1/tasks/../etc", self.submit_token)[0], 400)
        self.assertEqual(self._raw("POST", "/v1/tasks/bad%20id", self.submit_token, b"")[0], 400)

    def test_full_lifecycle(self):
        rec = self.a.submit("t1", {"title": "first", "lease_seconds": 300, "max_attempts": 1}, b"BUNDLE")
        self.assertEqual(rec.status, STATUS_QUEUED)
        with self.assertRaises(TransportError):
            self.a.submit("t1", {}, b"dup")
        with self.assertRaises(TransportError):
            self.a.get_result("t1")
        claimed = self.b.claim("laptop-b")
        self.assertEqual(claimed.status, STATUS_CLAIMED)
        self.assertEqual(claimed.meta["title"], "first")
        self.assertIsNone(self.b.claim("laptop-b"))
        self.assertEqual(self.b.get_bundle("t1"), b"BUNDLE")
        with self.assertRaises(TransportError):
            self.a.get_bundle("t1")  # submit role cannot download bundles
        rec = self.b.heartbeat("t1", "laptop-b", status="running", message="agent")
        self.assertEqual(rec.status, "running")
        with self.assertRaises(TransportError):
            self.b.heartbeat("t1", "impostor")
        rec = self.b.upload_result("t1", "laptop-b", {"outcome": "success"}, b"RESULT", STATUS_SUCCEEDED)
        self.assertEqual(rec.status, STATUS_SUCCEEDED)
        self.assertEqual(self.a.get_result("t1"), b"RESULT")
        self.assertEqual(self.a.get("t1").result_meta["outcome"], "success")
        self.assertEqual([r.task_id for r in self.a.list(status=STATUS_SUCCEEDED)], ["t1"])

    def test_cancel_blocks_claim_and_upload(self):
        self.a.submit("t2", {}, b"x")
        self.assertEqual(self.a.cancel("t2").status, STATUS_CANCELLED)
        self.assertIsNone(self.b.claim("w"))
        self.a.submit("t3", {}, b"x")
        self.b.claim("w")
        self.a.cancel("t3")
        self.assertEqual(self.b.heartbeat("t3", "w").status, STATUS_CANCELLED)
        with self.assertRaises(TransportError):
            self.b.upload_result("t3", "w", {}, b"r", STATUS_SUCCEEDED)

    def test_concurrent_claims_are_distinct(self):
        for i in range(6):
            self.a.submit(f"c{i}", {}, b"x")
        got, lock = [], threading.Lock()

        def claim_all():
            while True:
                r = self.b.claim(threading.current_thread().name)
                if r is None:
                    return
                with lock:
                    got.append(r.task_id)

        threads = [threading.Thread(target=claim_all, name=f"w{i}") for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(got), [f"c{i}" for i in range(6)])
        self.assertEqual(len(set(got)), 6)

    def test_lease_expiry_requeues(self):
        self.a.submit("t4", {"lease_seconds": 1, "max_attempts": 2}, b"x")
        first = self.b.claim("w1")
        self.assertEqual(first.attempts, 1)
        # Force the lease into the past directly in the store.
        self.server.store._db.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id='t4'")
        second = self.b.claim("w2")
        self.assertIsNotNone(second)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(second.worker_id, "w2")
