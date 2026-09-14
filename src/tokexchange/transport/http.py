"""HTTP client for the bundled coordinator (see :mod:`tokexchange.coordinator.server`)."""
from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import TaskRecord, Transport, TransportError

META_HEADER = "X-Tokexchange-Meta"


def make_ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that also works on Python builds without a CA bundle.

    Order: ``TOKEXCHANGE_CA_BUNDLE`` (for a self-signed coordinator cert), the default
    context if it already has CAs, ``certifi`` when installed, then the macOS/Linux
    system bundle. Verification is never disabled.
    """
    custom = os.environ.get("TOKEXCHANGE_CA_BUNDLE")
    if custom:
        return ssl.create_default_context(cafile=custom)
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509", 0) > 0:
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for candidate in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.isfile(candidate):
            return ssl.create_default_context(cafile=candidate)
    return ctx


class HttpTransport(Transport):
    def __init__(self, base_url: str, token: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl = make_ssl_context() if self.base_url.startswith("https://") else None

    # -- low level -----------------------------------------------------------
    def _request(self, method: str, path: str, *, body: bytes | None = None, json_body: Any = None,
                 headers: dict[str, str] | None = None, query: dict[str, Any] | None = None) -> tuple[int, bytes, dict]:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        hdrs = {"Authorization": f"Bearer {self.token}", "User-Agent": "tokexchange"}
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif body is not None:
            hdrs["Content-Type"] = "application/octet-stream"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except Exception:
                pass
            raise TransportError(f"{method} {path} -> {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in str(exc.reason):
                hint = (" (TLS verification failed: run 'pip install certifi', or on a python.org macOS build run "
                        "'Install Certificates.command'; for a self-signed coordinator set TOKEXCHANGE_CA_BUNDLE)")
            raise TransportError(f"cannot reach coordinator at {self.base_url}: {exc.reason}{hint}") from None

    def _json(self, method: str, path: str, **kw) -> Any:
        status, data, _ = self._request(method, path, **kw)
        if status == 204 or not data:
            return None
        return json.loads(data.decode())

    @staticmethod
    def _encode_meta(meta: dict[str, Any]) -> str:
        return base64.b64encode(json.dumps(meta).encode()).decode()

    # -- submitter -----------------------------------------------------------
    def submit(self, task_id: str, meta: dict[str, Any], bundle: bytes) -> TaskRecord:
        rec = self._json("POST", f"/v1/tasks/{task_id}", body=bundle, headers={META_HEADER: self._encode_meta(meta)})
        return TaskRecord.from_dict(rec)

    def get(self, task_id: str) -> TaskRecord:
        return TaskRecord.from_dict(self._json("GET", f"/v1/tasks/{task_id}"))

    def list(self, status: str | None = None, limit: int = 100) -> list[TaskRecord]:
        data = self._json("GET", "/v1/tasks", query={"status": status, "limit": limit})
        return [TaskRecord.from_dict(r) for r in data["tasks"]]

    def get_result(self, task_id: str) -> bytes:
        _, data, _ = self._request("GET", f"/v1/tasks/{task_id}/result")
        return data

    def cancel(self, task_id: str) -> TaskRecord:
        return TaskRecord.from_dict(self._json("POST", f"/v1/tasks/{task_id}/cancel", json_body={}))

    # -- worker --------------------------------------------------------------
    def claim(self, worker_id: str, lease_seconds: int | None = None) -> TaskRecord | None:
        data = self._json("POST", "/v1/tasks/claim", json_body={"worker_id": worker_id, "lease_seconds": lease_seconds})
        return TaskRecord.from_dict(data) if data else None

    def get_bundle(self, task_id: str) -> bytes:
        _, data, _ = self._request("GET", f"/v1/tasks/{task_id}/bundle")
        return data

    def heartbeat(self, task_id: str, worker_id: str, status: str | None = None, message: str | None = None) -> TaskRecord:
        data = self._json("POST", f"/v1/tasks/{task_id}/heartbeat",
                          json_body={"worker_id": worker_id, "status": status, "message": message})
        return TaskRecord.from_dict(data)

    def upload_result(self, task_id: str, worker_id: str, result_meta: dict[str, Any], result: bytes, final_status: str) -> TaskRecord:
        meta = {"worker_id": worker_id, "final_status": final_status, "result_meta": result_meta}
        data = self._json("PUT", f"/v1/tasks/{task_id}/result", body=result, headers={META_HEADER: self._encode_meta(meta)})
        return TaskRecord.from_dict(data)
