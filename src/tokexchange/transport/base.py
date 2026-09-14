from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class TransportError(RuntimeError):
    pass


@dataclass
class TaskRecord:
    """Coordinator-side view of a task: metadata plus lifecycle state, never the payload."""

    task_id: str
    status: str
    meta: dict[str, Any]
    created_at: str
    updated_at: str
    attempts: int = 0
    worker_id: str | None = None
    lease_expires_at: str | None = None
    message: str | None = None
    result_meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskRecord":
        return cls(
            task_id=d["task_id"], status=d["status"], meta=d.get("meta", {}),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
            attempts=int(d.get("attempts", 0)), worker_id=d.get("worker_id"),
            lease_expires_at=d.get("lease_expires_at"), message=d.get("message"),
            result_meta=d.get("result_meta") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "status": self.status, "meta": self.meta,
            "created_at": self.created_at, "updated_at": self.updated_at, "attempts": self.attempts,
            "worker_id": self.worker_id, "lease_expires_at": self.lease_expires_at,
            "message": self.message, "result_meta": self.result_meta,
        }


class Transport:
    """Operations both laptops need. Payloads are opaque bytes (possibly encrypted)."""

    # submitter side
    def submit(self, task_id: str, meta: dict[str, Any], bundle: bytes) -> TaskRecord:
        raise NotImplementedError

    def get(self, task_id: str) -> TaskRecord:
        raise NotImplementedError

    def list(self, status: str | None = None, limit: int = 100) -> list[TaskRecord]:
        raise NotImplementedError

    def get_result(self, task_id: str) -> bytes:
        raise NotImplementedError

    def cancel(self, task_id: str) -> TaskRecord:
        raise NotImplementedError

    # worker side
    def claim(self, worker_id: str, lease_seconds: int | None = None) -> TaskRecord | None:
        raise NotImplementedError

    def get_bundle(self, task_id: str) -> bytes:
        raise NotImplementedError

    def heartbeat(self, task_id: str, worker_id: str, status: str | None = None, message: str | None = None) -> TaskRecord:
        raise NotImplementedError

    def upload_result(self, task_id: str, worker_id: str, result_meta: dict[str, Any], result: bytes, final_status: str) -> TaskRecord:
        raise NotImplementedError
