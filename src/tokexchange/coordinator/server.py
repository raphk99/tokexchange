"""Coordinator service.

Standard library only: ``ThreadingHTTPServer`` + ``sqlite3``. Payloads (task
bundles, results) are stored as opaque blobs on disk; the database holds only
metadata and lifecycle state. Authentication is a bearer token per device with
roles ``submit`` and/or ``work``. Put it behind TLS (built-in ``--tls-cert`` /
``--tls-key``, or a reverse proxy such as Caddy, or a Tailscale/Fly.io deployment).

REST surface (all JSON unless noted):

    POST /v1/tasks/{id}            raw bundle body + X-Tokexchange-Meta (b64 json)   [submit]
    GET  /v1/tasks?status=&limit=                                                   [submit|work]
    GET  /v1/tasks/{id}                                                             [submit|work]
    POST /v1/tasks/{id}/cancel                                                      [submit]
    GET  /v1/tasks/{id}/result     raw bytes                                        [submit]
    POST /v1/tasks/claim           {worker_id, lease_seconds}  -> record | 204      [work]
    GET  /v1/tasks/{id}/bundle     raw bytes                                        [work]
    POST /v1/tasks/{id}/heartbeat  {worker_id, status?, message?}                   [work]
    PUT  /v1/tasks/{id}/result     raw result body + X-Tokexchange-Meta             [work]
    GET  /v1/health                                                                 [any]
"""
from __future__ import annotations

import base64
import datetime as _dt
import hmac
import json
import re
import secrets
import sqlite3
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..models import (STATUS_CANCELLED, STATUS_CLAIMED, STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING,
                      STATUS_SUCCEEDED, TERMINAL_STATUSES, now_iso)
from ..transport.base import TaskRecord
from ..transport.http import META_HEADER

MAX_BODY = 512 * 1024 * 1024  # 512 MiB
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

class TokenStore:
    """``{"tokens": {"<token>": {"name": "laptop-a", "roles": ["submit"]}}}``."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            if self.path.is_file():
                self._data = json.loads(self.path.read_text())
            else:
                self._data = {"tokens": {}}

    def authenticate(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            for stored, info in self._data.get("tokens", {}).items():
                if hmac.compare_digest(stored, token):
                    return {"name": info.get("name", "?"), "roles": set(info.get("roles", []))}
        return None

    @classmethod
    def add_token(cls, path: Path, name: str, roles: list[str]) -> str:
        token = secrets.token_urlsafe(32)
        data = json.loads(path.read_text()) if path.is_file() else {"tokens": {}}
        data.setdefault("tokens", {})[token] = {"name": name, "roles": roles, "created_at": now_iso()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.blobs = data_dir / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(data_dir / "tasks.db"), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL, meta TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT, lease_expires_at TEXT, message TEXT, result_meta TEXT,
                owner TEXT, max_attempts INTEGER NOT NULL DEFAULT 2, lease_seconds INTEGER NOT NULL DEFAULT 3600)""")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"], status=row["status"], meta=json.loads(row["meta"]),
            created_at=row["created_at"], updated_at=row["updated_at"], attempts=row["attempts"],
            worker_id=row["worker_id"], lease_expires_at=row["lease_expires_at"], message=row["message"],
            result_meta=json.loads(row["result_meta"]) if row["result_meta"] else {},
        )

    def _fetch(self, task_id: str) -> TaskRecord:
        self._db.row_factory = sqlite3.Row
        row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, f"unknown task {task_id}")
        return self._row_to_record(row)

    def create(self, task_id: str, meta: dict[str, Any], bundle: bytes, owner: str) -> TaskRecord:
        with self._lock:
            if self._db.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                raise ApiError(409, f"task {task_id} already exists")
            (self.blobs / f"{task_id}.bundle").write_bytes(bundle)
            ts = now_iso()
            self._db.execute(
                "INSERT INTO tasks (task_id,status,meta,created_at,updated_at,owner,max_attempts,lease_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, STATUS_QUEUED, json.dumps(meta), ts, ts, owner,
                 int(meta.get("max_attempts") or 2), int(meta.get("lease_seconds") or 3600)))
            return self._fetch(task_id)

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            return self._fetch(task_id)

    def list(self, status: str | None, limit: int) -> list[TaskRecord]:
        with self._lock:
            self._db.row_factory = sqlite3.Row
            if status:
                rows = self._db.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._row_to_record(r) for r in rows]

    def cancel(self, task_id: str) -> TaskRecord:
        with self._lock:
            rec = self._fetch(task_id)
            if rec.status not in TERMINAL_STATUSES:
                self._db.execute("UPDATE tasks SET status=?, updated_at=?, message=? WHERE task_id=?",
                                 (STATUS_CANCELLED, now_iso(), "cancelled by submitter", task_id))
            return self._fetch(task_id)

    def _expire_leases(self) -> None:
        now = now_iso()
        self._db.row_factory = sqlite3.Row
        rows = self._db.execute(
            "SELECT * FROM tasks WHERE status IN (?,?) AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (STATUS_CLAIMED, STATUS_RUNNING, now)).fetchall()
        for row in rows:
            if row["attempts"] >= row["max_attempts"]:
                self._db.execute("UPDATE tasks SET status=?, updated_at=?, message=?, lease_expires_at=NULL WHERE task_id=?",
                                 (STATUS_FAILED, now, "lease expired; max attempts reached", row["task_id"]))
            else:
                self._db.execute("UPDATE tasks SET status=?, updated_at=?, message=?, worker_id=NULL, lease_expires_at=NULL WHERE task_id=?",
                                 (STATUS_QUEUED, now, f"lease expired for worker {row['worker_id']}; requeued", row["task_id"]))

    def claim(self, worker_id: str, lease_seconds: int | None) -> TaskRecord | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._expire_leases()
                self._db.row_factory = sqlite3.Row
                row = self._db.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at ASC LIMIT 1", (STATUS_QUEUED,)).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                lease = int(lease_seconds or row["lease_seconds"])
                expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=lease)).replace(microsecond=0).isoformat()
                self._db.execute(
                    "UPDATE tasks SET status=?, worker_id=?, attempts=attempts+1, lease_expires_at=?, updated_at=?, message=NULL WHERE task_id=?",
                    (STATUS_CLAIMED, worker_id, expires, now_iso(), row["task_id"]))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return self._fetch(row["task_id"])

    def bundle(self, task_id: str) -> bytes:
        with self._lock:
            self._fetch(task_id)
        p = self.blobs / f"{task_id}.bundle"
        if not p.is_file():
            raise ApiError(404, "bundle missing")
        return p.read_bytes()

    def heartbeat(self, task_id: str, worker_id: str, status: str | None, message: str | None) -> TaskRecord:
        with self._lock:
            rec = self._fetch(task_id)
            if rec.worker_id != worker_id:
                raise ApiError(409, "task is not leased to this worker")
            if rec.status in TERMINAL_STATUSES:
                return rec
            if status and status not in (STATUS_CLAIMED, STATUS_RUNNING):
                raise ApiError(400, "heartbeat may only set claimed/running")
            self._db.row_factory = sqlite3.Row
            lease = self._db.execute("SELECT lease_seconds FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=int(lease))).replace(microsecond=0).isoformat()
            self._db.execute("UPDATE tasks SET lease_expires_at=?, updated_at=?, status=COALESCE(?, status), message=COALESCE(?, message) WHERE task_id=?",
                             (expires, now_iso(), status, message, task_id))
            return self._fetch(task_id)

    def upload_result(self, task_id: str, worker_id: str, result_meta: dict[str, Any], result: bytes, final_status: str) -> TaskRecord:
        if final_status not in (STATUS_SUCCEEDED, STATUS_FAILED):
            raise ApiError(400, "final_status must be succeeded or failed")
        with self._lock:
            rec = self._fetch(task_id)
            if rec.worker_id != worker_id:
                raise ApiError(409, "task is not leased to this worker")
            if rec.status == STATUS_CANCELLED:
                raise ApiError(409, "task was cancelled")
            (self.blobs / f"{task_id}.result").write_bytes(result)
            self._db.execute("UPDATE tasks SET status=?, result_meta=?, lease_expires_at=NULL, updated_at=?, message=? WHERE task_id=?",
                             (final_status, json.dumps(result_meta), now_iso(), result_meta.get("outcome"), task_id))
            return self._fetch(task_id)

    def close(self) -> None:
        self._db.close()

    def result(self, task_id: str) -> bytes:
        with self._lock:
            rec = self._fetch(task_id)
        p = self.blobs / f"{task_id}.result"
        if not p.is_file():
            raise ApiError(404, f"task {task_id} has no result yet (status: {rec.status})")
        return p.read_bytes()


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def make_handler(store: Store, tokens: TokenStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tokexchange/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter default logging
            if getattr(self.server, "verbose", False):
                super().log_message(fmt, *args)

        # helpers ------------------------------------------------------------
        def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode())

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "body too large")
            return self.rfile.read(length) if length else b""

        def _meta_header(self) -> dict[str, Any]:
            raw = self.headers.get(META_HEADER)
            if not raw:
                raise ApiError(400, f"missing {META_HEADER} header")
            try:
                return json.loads(base64.b64decode(raw).decode())
            except Exception:
                raise ApiError(400, f"invalid {META_HEADER} header")

        def _auth(self, *roles: str) -> dict[str, Any]:
            header = self.headers.get("Authorization") or ""
            token = header[7:] if header.startswith("Bearer ") else None
            principal = tokens.authenticate(token)
            if principal is None:
                raise ApiError(401, "invalid or missing token")
            if roles and not (principal["roles"] & set(roles)):
                raise ApiError(403, f"token lacks role: one of {sorted(roles)}")
            return principal

        def _route(self, method: str) -> None:
            try:
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                self._dispatch(method, parts, query)
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except Exception as exc:  # pragma: no cover - defensive
                self._json(500, {"error": f"internal error: {exc}"})

        def _dispatch(self, method: str, parts: list[str], query: dict[str, str]) -> None:
            if parts == ["v1", "health"] and method == "GET":
                self._auth()
                return self._json(200, {"ok": True, "time": now_iso()})
            if len(parts) < 2 or parts[0] != "v1" or parts[1] != "tasks":
                raise ApiError(404, "not found")
            if parts == ["v1", "tasks"] and method == "GET":
                self._auth("submit", "work")
                limit = min(int(query.get("limit") or 100), 1000)
                return self._json(200, {"tasks": [r.to_dict() for r in store.list(query.get("status"), limit)]})
            if parts == ["v1", "tasks", "claim"] and method == "POST":
                principal = self._auth("work")
                body = json.loads(self._body() or b"{}")
                worker_id = str(body.get("worker_id") or principal["name"])
                rec = store.claim(worker_id, body.get("lease_seconds"))
                return self._json(200, rec.to_dict()) if rec else self._send(204)
            task_id = parts[2] if len(parts) >= 3 else None
            if not task_id or not TASK_ID_RE.match(task_id):
                raise ApiError(400, "invalid task id")
            sub = parts[3] if len(parts) >= 4 else None
            if sub is None:
                if method == "POST":
                    principal = self._auth("submit")
                    meta = self._meta_header()
                    return self._json(201, store.create(task_id, meta, self._body(), principal["name"]).to_dict())
                if method == "GET":
                    self._auth("submit", "work")
                    return self._json(200, store.get(task_id).to_dict())
            if sub == "cancel" and method == "POST":
                self._auth("submit")
                return self._json(200, store.cancel(task_id).to_dict())
            if sub == "bundle" and method == "GET":
                self._auth("work")
                return self._send(200, store.bundle(task_id), "application/octet-stream")
            if sub == "heartbeat" and method == "POST":
                principal = self._auth("work")
                body = json.loads(self._body() or b"{}")
                rec = store.heartbeat(task_id, str(body.get("worker_id") or principal["name"]), body.get("status"), body.get("message"))
                return self._json(200, rec.to_dict())
            if sub == "result":
                if method == "PUT":
                    principal = self._auth("work")
                    meta = self._meta_header()
                    rec = store.upload_result(task_id, str(meta.get("worker_id") or principal["name"]),
                                              meta.get("result_meta") or {}, self._body(), str(meta.get("final_status")))
                    return self._json(200, rec.to_dict())
                if method == "GET":
                    self._auth("submit")
                    return self._send(200, store.result(task_id), "application/octet-stream")
            raise ApiError(404, "not found")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return Handler


class CoordinatorServer:
    def __init__(self, data_dir: Path, tokens_file: Path, host: str = "127.0.0.1", port: int = 8787,
                 tls_cert: Path | None = None, tls_key: Path | None = None, verbose: bool = False):
        self.store = Store(data_dir)
        self.tokens = TokenStore(tokens_file)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.store, self.tokens))
        self.httpd.daemon_threads = True
        self.httpd.verbose = verbose  # type: ignore[attr-defined]
        self.scheme = "http"
        if tls_cert and tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(tls_cert), str(tls_key))
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"{self.scheme}://{host}:{port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
