"""Shared-directory transport.

Layout: ``<root>/tasks/<task_id>/{record.json,bundle.bin,result.bin}``. Claiming
uses an exclusive ``claim.lock`` file created with O_EXCL, which is atomic on a
local filesystem. Good for tests and for a folder synced by Dropbox/iCloud/Syncthing
when you would rather not run a coordinator at all (with the caveat that sync
clients do not provide atomic locks across machines: run a single worker).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from ..models import (STATUS_CANCELLED, STATUS_CLAIMED, STATUS_QUEUED, STATUS_RUNNING, TERMINAL_STATUSES, now_iso)
from .base import TaskRecord, Transport, TransportError


class LocalDirTransport(Transport):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "tasks").mkdir(parents=True, exist_ok=True)

    def _dir(self, task_id: str) -> Path:
        if "/" in task_id or task_id in ("", ".", ".."):
            raise TransportError("invalid task id")
        return self.root / "tasks" / task_id

    def _read(self, task_id: str) -> TaskRecord:
        p = self._dir(task_id) / "record.json"
        if not p.is_file():
            raise TransportError(f"unknown task {task_id}")
        return TaskRecord.from_dict(json.loads(p.read_text()))

    def _write(self, rec: TaskRecord) -> None:
        rec.updated_at = now_iso()
        p = self._dir(rec.task_id) / "record.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec.to_dict(), indent=2))
        os.replace(tmp, p)

    def submit(self, task_id: str, meta: dict[str, Any], bundle: bytes) -> TaskRecord:
        d = self._dir(task_id)
        if d.exists():
            raise TransportError(f"task {task_id} already exists")
        d.mkdir(parents=True)
        (d / "bundle.bin").write_bytes(bundle)
        rec = TaskRecord(task_id=task_id, status=STATUS_QUEUED, meta=meta, created_at=now_iso(), updated_at=now_iso())
        self._write(rec)
        return rec

    def get(self, task_id: str) -> TaskRecord:
        return self._read(task_id)

    def list(self, status: str | None = None, limit: int = 100) -> list[TaskRecord]:
        recs = []
        for d in (self.root / "tasks").iterdir():
            if (d / "record.json").is_file():
                rec = self._read(d.name)
                if status is None or rec.status == status:
                    recs.append(rec)
        recs.sort(key=lambda r: r.created_at, reverse=True)
        return recs[:limit]

    def get_result(self, task_id: str) -> bytes:
        p = self._dir(task_id) / "result.bin"
        if not p.is_file():
            raise TransportError(f"task {task_id} has no result yet")
        return p.read_bytes()

    def cancel(self, task_id: str) -> TaskRecord:
        rec = self._read(task_id)
        if rec.status not in TERMINAL_STATUSES:
            rec.status = STATUS_CANCELLED
            self._write(rec)
        return rec

    def claim(self, worker_id: str, lease_seconds: int | None = None) -> TaskRecord | None:
        self._expire_leases()
        for rec in sorted(self.list(STATUS_QUEUED), key=lambda r: r.created_at):
            lock = self._dir(rec.task_id) / "claim.lock"
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(fd)
            rec = self._read(rec.task_id)
            if rec.status != STATUS_QUEUED:
                continue
            lease = lease_seconds or int(rec.meta.get("lease_seconds") or 3600)
            rec.status = STATUS_CLAIMED
            rec.worker_id = worker_id
            rec.attempts += 1
            rec.lease_expires_at = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=lease)).replace(microsecond=0).isoformat()
            self._write(rec)
            return rec
        return None

    def _expire_leases(self) -> None:
        now = now_iso()
        for rec in self.list():
            if rec.status in (STATUS_CLAIMED, STATUS_RUNNING) and rec.lease_expires_at and rec.lease_expires_at < now:
                lock = self._dir(rec.task_id) / "claim.lock"
                lock.unlink(missing_ok=True)
                if rec.attempts >= int(rec.meta.get("max_attempts") or 2):
                    rec.status = "failed"
                    rec.message = "lease expired; max attempts reached"
                else:
                    rec.status = STATUS_QUEUED
                    rec.message = "lease expired; requeued"
                    rec.worker_id = None
                    rec.lease_expires_at = None
                self._write(rec)

    def get_bundle(self, task_id: str) -> bytes:
        return (self._dir(task_id) / "bundle.bin").read_bytes()

    def heartbeat(self, task_id: str, worker_id: str, status: str | None = None, message: str | None = None) -> TaskRecord:
        rec = self._read(task_id)
        if rec.worker_id != worker_id:
            raise TransportError("task is not leased to this worker")
        if rec.status == STATUS_CANCELLED:
            return rec
        lease = int(rec.meta.get("lease_seconds") or 3600)
        rec.lease_expires_at = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=lease)).replace(microsecond=0).isoformat()
        if status:
            rec.status = status
        if message is not None:
            rec.message = message
        self._write(rec)
        return rec

    def upload_result(self, task_id: str, worker_id: str, result_meta: dict[str, Any], result: bytes, final_status: str) -> TaskRecord:
        rec = self._read(task_id)
        if rec.worker_id != worker_id:
            raise TransportError("task is not leased to this worker")
        (self._dir(task_id) / "result.bin").write_bytes(result)
        rec.result_meta = result_meta
        rec.status = final_status
        rec.lease_expires_at = None
        self._write(rec)
        return rec
