"""S3 storage backend (AWS Signature V4, standard library only).

Works against AWS S3 and S3-compatible services (Yandex Object Storage, MinIO,
Cloudflare R2, Ceph RGW, LocalStack) through ``endpoint_url``.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from ..config import S3Config
from ..errors import NotFound, StorageError
from ..sigv4 import (
    EMPTY_SHA256,
    UNSIGNED_PAYLOAD,
    canonical_querystring,
    canonical_uri,
    sha256_file,
    sign_request,
)
from .base import ObjectMeta, ProbeResult, Storage, StreamHandle

_RETRY_STATUS = (500, 502, 503, 504)
_META_HEADER = "x-amz-meta-"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class S3Storage(Storage):
    name = "s3"
    distributed = True

    def __init__(
        self,
        config: S3Config,
        *,
        opener: Optional[Callable[..., object]] = None,
        retries: int = 2,
        timeout: float = 60.0,
        probe_timeout: float = 5.0,
    ) -> None:
        config.validate()
        self.cfg = config
        self.path_style = config.resolved_path_style()
        self._opener = opener or urllib.request.urlopen
        self.retries = retries
        self.timeout = timeout
        #: A health probe must answer long before a normal object operation would.
        self.probe_timeout = probe_timeout if probe_timeout > 0 else timeout

    # ---- addressing --------------------------------------------------------
    def _base_url(self) -> str:
        if self.cfg.endpoint_url:
            scheme, sep, rest = self.cfg.endpoint_url.rstrip("/").partition("://")
            rest = rest.rstrip("/")
            if self.path_style:
                return f"{scheme}{sep}{rest}/{self.cfg.bucket}"
            return f"{scheme}://{self.cfg.bucket}.{rest}"
        return f"https://{self.cfg.bucket}.s3.{self.cfg.region}.amazonaws.com"

    def _host_and_path(self, key: str = "") -> tuple[str, str, str]:
        """Return ``(scheme, host, raw_path)``; the path is *not* percent-encoded yet."""
        if self.cfg.endpoint_url:
            scheme, _sep, rest = self.cfg.endpoint_url.rstrip("/").partition("://")
            if self.path_style:
                host, path = rest, f"/{self.cfg.bucket}"
            else:
                host, path = f"{self.cfg.bucket}.{rest}", ""
        else:
            scheme = "https"
            host = f"{self.cfg.bucket}.s3.{self.cfg.region}.amazonaws.com"
            path = ""
        if key:
            path = f"{path}/{key}"
        return scheme, host, (path or "/")

    def object_url(self, key: str = "") -> str:
        scheme, host, raw_path = self._host_and_path(key)
        return f"{scheme}://{host}{canonical_uri(raw_path)}"

    def full_key(self, key: str) -> str:
        if not key:
            return ""
        return f"{self.cfg.prefix}/{key}" if self.cfg.prefix else key

    # ---- transport ---------------------------------------------------------
    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: Optional[dict[str, str]] = None,
        data=None,
        headers: Optional[dict[str, str]] = None,
        content_length: Optional[int] = None,
        payload_hash: str = EMPTY_SHA256,
        timeout: Optional[float] = None,
    ):
        socket_timeout = timeout or self.timeout
        scheme, host, raw_path = self._host_and_path(self.full_key(key))
        canon_path = canonical_uri(raw_path)
        canon_query = canonical_querystring(query)
        url = f"{scheme}://{host}{canon_path}" + (f"?{canon_query}" if canon_query else "")
        headers = dict(headers or {})
        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        attempt = 0
        while True:
            # A retry must replay the body from the start: urllib leaves the file pointer
            # wherever the failed attempt stopped, so a second send would upload nothing.
            rewind(data)
            signed = sign_request(
                method,
                url,
                region=self.cfg.region,
                service="s3",
                access_key=self.cfg.access_key or "",
                secret_key=self.cfg.secret_key or "",
                session_token=self.cfg.session_token,
                headers=headers,
                payload_hash=payload_hash,
                canonical_path=canon_path,
                canonical_query=canon_query,
            )
            request = urllib.request.Request(url, data=data, method=method)
            for name, value in signed.items():
                request.add_header(name, value)
            try:
                return self._opener(request, timeout=socket_timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in _RETRY_STATUS and attempt < self.retries:
                    # Release the failed response before retrying, or its connection stays
                    # checked out of the pool for the life of the process.
                    exc.close()
                    attempt += 1
                    time.sleep(min(0.4 * attempt, 2.0))
                    continue
                raise
            except urllib.error.URLError as exc:
                if attempt < self.retries and replayable(data):
                    attempt += 1
                    time.sleep(min(0.4 * attempt, 2.0))
                    continue
                raise StorageError(f"S3 {method} {key!r} failed: {exc}") from exc

    # ---- read --------------------------------------------------------------
    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    def stat(self, key: str) -> Optional[ObjectMeta]:
        try:
            response = self._request("HEAD", key)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise StorageError(f"S3 HEAD {key!r} failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise StorageError(f"S3 HEAD {key!r} failed: {exc}") from exc
        with response:
            headers = {k.lower(): v for k, v in response.headers.items()}
        return self._meta_from_headers(key, headers)

    def _meta_from_headers(self, key: str, headers: dict[str, str]) -> ObjectMeta:
        extra = {name[len(_META_HEADER):]: value for name, value in headers.items() if name.startswith(_META_HEADER)}
        size = headers.get("content-length")
        fetched_at = extra.get("fetched-at")
        if fetched_at is None and headers.get("last-modified"):
            fetched_at = str(_http_date(headers["last-modified"]))
        return ObjectMeta(
            key=key,
            size=int(size) if size and size.isdigit() else None,
            sha256=extra.pop("sha256", None),
            content_type=headers.get("content-type"),
            fetched_at=float(fetched_at) if fetched_at else None,
            backend=self.name,
            extra=extra,
        )

    def open(self, key: str) -> StreamHandle:
        try:
            response = self._request("GET", key)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(f"object {key!r} is not cached") from exc
            raise StorageError(f"S3 GET {key!r} failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise StorageError(f"S3 GET {key!r} failed: {exc}") from exc
        headers = {k.lower(): v for k, v in response.headers.items()}
        meta = self._meta_from_headers(key, headers)

        def read(size: int) -> bytes:
            return response.read(size)

        return StreamHandle(meta, read, response.close)

    # ---- write -------------------------------------------------------------
    def _meta_headers(self, sha256: Optional[str], meta: Optional[dict[str, str]]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if sha256:
            headers[_META_HEADER + "sha256"] = sha256
        headers[_META_HEADER + "fetched-at"] = f"{time.time():.3f}"
        for name, value in (meta or {}).items():
            headers[_META_HEADER + _safe_header(name)] = _safe_header(str(value))
        return headers

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta:
        headers = self._meta_headers(None, meta)
        if content_type:
            headers["Content-Type"] = content_type
        self._request(
            "PUT", key, data=data, headers=headers, content_length=len(data), payload_hash=_payload(data)
        )
        return ObjectMeta(
            key=key,
            size=len(data),
            sha256=_sha256(data),
            content_type=content_type,
            fetched_at=time.time(),
            backend=self.name,
            extra=dict(meta or {}),
        )

    def put_file(
        self,
        key: str,
        path: str,
        *,
        size: Optional[int] = None,
        sha256: Optional[str] = None,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> ObjectMeta:
        digest, size_on_disk = sha256_file(path)
        size = size or size_on_disk
        sha256 = sha256 or digest
        headers = self._meta_headers(sha256, meta)
        if content_type:
            headers["Content-Type"] = content_type
        payload_hash = UNSIGNED_PAYLOAD if (self.cfg.unsigned_payload and self._is_https()) else digest
        with open(path, "rb") as body:
            self._request(
                "PUT", key, data=body, headers=headers, content_length=size, payload_hash=payload_hash
            )
        return ObjectMeta(
            key=key,
            size=size,
            sha256=sha256,
            content_type=content_type,
            fetched_at=time.time(),
            backend=self.name,
            extra=dict(meta or {}),
        )

    def _is_https(self) -> bool:
        return self._base_url().startswith("https://")

    # ---- delete / list -----------------------------------------------------
    def put_new(
        self,
        key: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        meta: Optional[dict[str, str]] = None,
    ) -> bool:
        """Create an object only if it does not exist (used for distributed locks).

        Order of operations:

        1. cheap ``HEAD`` — if the object is there, back off without touching it, so a
           live lock is never clobbered;
        2. with ``conditional_locks`` send ``If-None-Match: *`` and let the store refuse
           the write (HTTP 412): strict mutual exclusion, supported by AWS S3 (since
           Nov 2024), MinIO, R2 and Ceph RGW;
        3. otherwise write and read the object back after a short settle delay, keeping
           the lock only if the stored bytes are still exactly ours. Two instances that
           race inside that window both see their own bytes, which is why the lock also
           carries a TTL and the payload hash makes a wrong winner harmless (both would
           store identical bytes).
        """
        if self.exists(key):
            return False
        headers = self._meta_headers(None, meta)
        if content_type:
            headers["Content-Type"] = content_type
        if self.cfg.conditional_locks:
            headers["If-None-Match"] = "*"
            try:
                self._request(
                    "PUT", key, data=data, headers=headers, content_length=len(data), payload_hash=_payload(data)
                )
            except urllib.error.HTTPError as exc:
                if exc.code in (304, 412):
                    return False
                raise StorageError(f"S3 conditional PUT {key!r} failed: HTTP {exc.code}") from exc
            return True
        self._request("PUT", key, data=data, headers=headers, content_length=len(data), payload_hash=_payload(data))
        if self.cfg.lock_verify_delay > 0:
            time.sleep(self.cfg.lock_verify_delay)
        body = self.read_or_none(key)
        return body is not None and body == data

    def delete(self, key: str) -> bool:
        try:
            with self._request("DELETE", key) as response:
                return response.status in (200, 204)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise StorageError(f"S3 DELETE {key!r} failed: HTTP {exc.code}") from exc

    def list(self, prefix: str = "") -> list[str]:
        wanted = self.full_key(prefix)
        strip = f"{self.cfg.prefix}/" if self.cfg.prefix else ""
        keys: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = wanted
            if token:
                query["continuation-token"] = token
            try:
                response = self._request("GET", "", query=query)
            except urllib.error.HTTPError as exc:
                raise StorageError(f"S3 LIST failed: HTTP {exc.code}") from exc
            with response:
                root = ET.fromstring(response.read())
            names = {name: value for name, value in _flatten(root)}
            page = [value for name, value in _flatten(root) if name == "Key"]
            keys.extend(value[len(strip):] if strip and value.startswith(strip) else value for value in page)
            truncated = names.get("IsTruncated", "false").lower() == "true"
            token = names.get("NextContinuationToken", "")
            if not truncated or not token:
                break
        return sorted(keys)

    def describe(self) -> dict[str, str]:
        return {
            "backend": self.name,
            "bucket": self.cfg.bucket,
            "region": self.cfg.region,
            "prefix": self.cfg.prefix,
            "endpoint": self.cfg.endpoint_url or "aws",
            "path_style": str(self.path_style),
        }

    # ---- health ------------------------------------------------------------
    def probe(self, write: bool = False) -> ProbeResult:
        """List one key: proves DNS, TLS, credentials and that the bucket exists.

        A ``HEAD`` on a known key answers 404 for a wrong bucket name too, so the probe
        uses ListObjectsV2 and reports the S3 error code (``AccessDenied``,
        ``NoSuchBucket``, ``SignatureDoesNotMatch``) instead of a bare status.
        """
        started = time.perf_counter()
        query = {"list-type": "2", "max-keys": "1"}
        if self.cfg.prefix:
            query["prefix"] = self.cfg.prefix
        ok, detail = True, ""
        try:
            with self._request("GET", "", query=query, timeout=self.probe_timeout) as response:
                detail = f"bucket {self.cfg.bucket} answered HTTP {getattr(response, 'status', 200)}"
        except urllib.error.HTTPError as exc:
            code = _s3_error_code(exc)
            exc.close()
            detail = f"HTTP {exc.code}" + (f" {code}" if code else "")
            ok = False
        except (urllib.error.URLError, StorageError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            ok, detail = False, f"{type(exc).__name__}: {reason}"
        if ok and write:
            ok, detail = self.probe_write()
        return ProbeResult(self.name, ok, detail or "reachable", write, time.perf_counter() - started)


def _flatten(root: ET.Element) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for element in root.iter():
        name = _localname(element.tag)
        if element.text and element.text.strip():
            out.append((name, element.text.strip()))
    return out


def _s3_error_code(exc: BaseException) -> str:
    """Pull ``<Code>`` out of an S3 error body: "AccessDenied" reads better than "HTTP 403"."""
    try:
        body = exc.read(2048)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - body already consumed or unreadable
        return ""
    match = re.search(rb"<Code>([^<]{1,64})</Code>", body or b"")
    return match.group(1).decode("ascii", "replace") if match else ""


def _safe_header(value: str) -> str:
    return "".join(char if 32 <= ord(char) < 127 else "_" for char in value)


def _payload(data: bytes) -> str:
    return _sha256(data) if data else EMPTY_SHA256


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _http_date(value: str) -> float:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):  # pragma: no cover - malformed header
        return 0.0


def rewind(data) -> None:
    """Put a seekable request body back to position zero (no-op for bytes).

    ``_request`` retries, and urllib leaves a file body wherever the failed attempt
    stopped; without this the retry would upload nothing.
    """
    seek = getattr(data, "seek", None)
    if seek is None:
        return
    try:
        seek(0)
    except (OSError, ValueError):  # pragma: no cover - unseekable body
        pass


def replayable(data) -> bool:
    """Can the request body be sent again? A stream that cannot seek must not be retried."""
    if data is None or isinstance(data, (bytes, bytearray, memoryview)):
        return True
    tell = getattr(data, "tell", None)
    if tell is None:
        return False
    try:
        tell()
    except (OSError, ValueError):  # pragma: no cover - unseekable body
        return False
    return True
