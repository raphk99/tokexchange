"""Pack and unpack task/result bundles (gzip tarballs with a manifest).

Extraction refuses absolute paths, ``..`` components, symlinks and hard links
so a malicious bundle cannot write outside its target directory.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import Iterable


class BundleError(ValueError):
    pass


def pack(entries: dict[str, bytes | Path]) -> bytes:
    """Create a tar.gz from ``{archive_path: bytes | file_path}``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(entries):
            _validate_name(name)
            data = entries[name]
            if isinstance(data, Path):
                tar.add(str(data), arcname=name, recursive=False)
            else:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _validate_name(name: str) -> None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or name.startswith("/") or not name:
        raise BundleError(f"unsafe archive path: {name!r}")


def unpack(data: bytes, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            _validate_name(member.name)
            if member.issym() or member.islnk() or member.isdev():
                raise BundleError(f"refusing special member: {member.name}")
            if member.isdir():
                (dest / member.name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target = dest / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            assert src is not None
            with target.open("wb") as fh:
                fh.write(src.read())
            written.append(member.name)
    return written


def read_json_member(data: bytes, name: str) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        try:
            member = tar.getmember(name)
        except KeyError as exc:
            raise BundleError(f"bundle has no {name}") from exc
        src = tar.extractfile(member)
        assert src is not None
        return json.loads(src.read().decode("utf-8"))


def list_members(data: bytes) -> Iterable[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return [m.name for m in tar.getmembers()]
