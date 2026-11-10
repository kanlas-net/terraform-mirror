"""Error types shared by the proxy layers."""

from __future__ import annotations

from typing import Optional


class ProxyError(Exception):
    """Base class for errors that can be reported to an HTTP client."""

    def __init__(self, status: int, code: str, message: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class BadRequest(ProxyError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(400, code, message)


class NotFound(ProxyError):
    def __init__(self, message: str = "not found", code: str = "not_found") -> None:
        super().__init__(404, code, message)


class Unauthorized(ProxyError):
    def __init__(self, message: str = "missing or invalid admin token") -> None:
        super().__init__(401, "unauthorized", message)


class UpstreamError(ProxyError):
    """The upstream registry answered with an error or could not be reached.

    ``reason`` is a short, bounded label for metrics (``unreachable``, ``http_503``,
    ``malformed``): the message is for a human and must never become a label value.
    """

    def __init__(self, status: int, message: str, code: str = "upstream_error", reason: str = "") -> None:
        super().__init__(status, code, message)
        self.reason = reason or "error"


class StorageError(Exception):
    """A storage backend failed in a way that is not a plain 'missing object'."""


class ConfigError(ValueError):
    """Invalid configuration."""
