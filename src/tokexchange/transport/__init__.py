"""Transports move task bundles and results between laptops via a coordinator.

``open_transport(url)`` picks an implementation by URL scheme:

* ``http://`` / ``https://`` – the bundled coordinator service (:mod:`tokexchange.coordinator`)
* ``file://``               – a shared directory (tests, or a synced folder such as Dropbox)
"""
from __future__ import annotations

from .base import Transport, TransportError, TaskRecord


def open_transport(url: str, token: str | None = None) -> Transport:
    if url.startswith("file://"):
        from .local import LocalDirTransport
        return LocalDirTransport(url[len("file://"):])
    if url.startswith(("http://", "https://")):
        from .http import HttpTransport
        if not token:
            raise TransportError("an API token is required for HTTP coordinators")
        return HttpTransport(url, token)
    raise TransportError(f"unsupported coordinator url: {url}")


__all__ = ["Transport", "TransportError", "TaskRecord", "open_transport"]
