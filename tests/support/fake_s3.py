"""A tiny in-process S3-compatible server used to exercise the S3 backend.

It implements just the REST surface the proxy uses (PUT/GET/HEAD/DELETE/ListObjectsV2),
stores objects in memory and *validates* the SigV4 headers it receives so the tests
catch signing regressions (missing headers, wrong payload hash, wrong scope).
"""

from __future__ import annotations

import hashlib
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

_AUTH_RE = re.compile(
    r"^AWS4-HMAC-SHA256 Credential=(?P<ak>[^/,]+)/(?P<date>\d{8})/(?P<region>[^/]+)/"
    r"(?P<service>[^/]+)/aws4_request, SignedHeaders=(?P<signed>[^,]+), Signature=(?P<sig>[0-9a-f]{64})$"
)


@dataclass
class StoredObject:
    body: bytes
    content_type: Optional[str] = None
    meta: dict[str, str] = field(default_factory=dict)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> "FakeS3":
        return self.server.fake  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:  # pragma: no cover - quiet tests
        return None

    # ---- plumbing ----------------------------------------------------------
    def _reply(self, status: int, body: bytes = b"", headers: Optional[dict[str, str]] = None) -> None:
        if self.command == "HEAD":
            body = b""
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _error(self, status: int, code: str) -> None:
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
            f"<Message>{code}</Message></Error>"
        ).encode("utf-8")
        self._reply(status, body, {"Content-Type": "application/xml"})

    def _target(self) -> tuple[str, str, dict[str, str]]:
        parts = urllib.parse.urlsplit(self.path)
        pieces = [p for p in parts.path.split("/") if p]
        bucket = pieces[0] if pieces else ""
        key = "/".join(pieces[1:])
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        return bucket, key, query

    def _check_auth(self) -> bool:
        fake = self.fake
        auth = self.headers.get("Authorization", "")
        match = _AUTH_RE.match(auth.strip())
        if not match:
            fake.auth_errors.append(f"malformed Authorization: {auth!r}")
            return False
        if match.group("ak") != fake.access_key:
            fake.auth_errors.append("unexpected access key")
            return False
        if match.group("region") != fake.region or match.group("service") != "s3":
            fake.auth_errors.append("wrong credential scope")
            return False
        signed = match.group("signed").split(";")
        required = ["host", "x-amz-date"]
        if self.headers.get("x-amz-content-sha256"):
            required.append("x-amz-content-sha256")
        if not all(name in signed for name in required):
            fake.auth_errors.append(f"SignedHeaders missing entries: {signed}")
            return False
        if not self.headers.get("x-amz-date"):
            fake.auth_errors.append("missing x-amz-date")
            return False
        if fake.session_token_required and not self.headers.get("x-amz-security-token"):
            fake.auth_errors.append("missing x-amz-security-token")
            return False
        return True

    def _check_payload(self, body: bytes) -> bool:
        declared = self.headers.get("x-amz-content-sha256")
        if declared is None:
            self.fake.auth_errors.append("missing x-amz-content-sha256")
            return False
        if declared == "UNSIGNED-PAYLOAD":
            if not self.fake.allow_unsigned:
                self.fake.auth_errors.append("UNSIGNED-PAYLOAD not allowed")
                return False
            return True
        if declared != hashlib.sha256(body).hexdigest():
            self.fake.auth_errors.append("x-amz-content-sha256 does not match the body")
            return False
        return True

    def _meta_headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items() if name.lower().startswith("x-amz-meta-")}

    # ---- verbs -------------------------------------------------------------
    def do_PUT(self) -> None:  # noqa: N802
        bucket, key, _ = self._target()
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        self.fake.requests.append(("PUT", key, dict(self.headers.items())))
        if not self._check_auth() or not self._check_payload(body):
            return self._error(403, "SignatureDoesNotMatch")
        if bucket != self.fake.bucket:
            return self._error(404, "NoSuchBucket")
        if self.headers.get("If-None-Match") == "*" and self.fake.get(bucket, key) is not None:
            return self._error(412, "PreconditionFailed")
        self.fake.put(bucket, key, body, self.headers.get("Content-Type"), self._meta_headers())
        self._reply(200, b"", {"ETag": f'"{hashlib.md5(body).hexdigest()}"'})

    def do_GET(self) -> None:  # noqa: N802
        bucket, key, query = self._target()
        self.fake.requests.append(("GET", key, dict(self.headers.items())))
        if not self._check_auth():
            return self._error(403, "SignatureDoesNotMatch")
        if bucket != self.fake.bucket:
            return self._error(404, "NoSuchBucket")
        if not key:
            if query.get("list-type") == "2":
                return self._list(query)
            return self._error(400, "InvalidRequest")
        stored = self.fake.get(bucket, key)
        if stored is None:
            return self._error(404, "NoSuchKey")
        headers = {"Content-Type": stored.content_type or "application/octet-stream"}
        headers.update(stored.meta)
        headers["ETag"] = f'"{hashlib.md5(stored.body).hexdigest()}"'
        self._reply(200, stored.body, headers)

    def do_HEAD(self) -> None:  # noqa: N802
        bucket, key, _ = self._target()
        self.fake.requests.append(("HEAD", key, dict(self.headers.items())))
        if not self._check_auth():
            return self._error(403, "SignatureDoesNotMatch")
        stored = self.fake.get(bucket, key)
        if stored is None:
            return self._error(404, "NoSuchKey")
        headers = {"Content-Type": stored.content_type or "application/octet-stream"}
        headers.update(stored.meta)
        self.send_response(200)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(stored.body)))
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        bucket, key, _ = self._target()
        self.fake.requests.append(("DELETE", key, dict(self.headers.items())))
        if not self._check_auth():
            return self._error(403, "SignatureDoesNotMatch")
        if self.fake.delete(bucket, key):
            return self._reply(204)
        self._error(404, "NoSuchKey")

    def _list(self, query: dict[str, str]) -> None:
        prefix = query.get("prefix", "")
        keys = [k for k in self.fake.objects if k.startswith(prefix)]
        page_size = int(query.get("max-keys", "1000"))
        start = int(query.get("continuation-token", "0") or "0")
        page = keys[start : start + page_size]
        truncated = start + page_size < len(keys)
        chunks = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
            f"<Name>{self.fake.bucket}</Name><Prefix>{prefix}</Prefix>",
            f"<KeyCount>{len(page)}</KeyCount><IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
        ]
        if truncated:
            chunks.append(f"<NextContinuationToken>{start + page_size}</NextContinuationToken>")
        for key in page:
            chunks.append(f"<Contents><Key>{key}</Key><Size>{len(self.fake.objects[key].body)}</Size></Contents>")
        chunks.append("</ListBucketResult>")
        self._reply(200, "".join(chunks).encode("utf-8"), {"Content-Type": "application/xml"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeS3:
    def __init__(self, bucket: str = "tf-cache", region: str = "ru-central1", access_key: str = "AKITEST") -> None:
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.session_token_required = False
        self.allow_unsigned = True
        self.objects: dict[str, StoredObject] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.auth_errors: list[str] = []
        self._failures: list[int] = []
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-s3", daemon=True)
        self.port = self._server.server_address[1]

    def start(self) -> "FakeS3":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def endpoint_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- store -------------------------------------------------------------
    def put(self, bucket: str, key: str, body: bytes, content_type: Optional[str], meta: dict[str, str]) -> None:
        with self._lock:
            self.objects[key] = StoredObject(
                body=body,
                content_type=content_type,
                meta={k.lower(): v for k, v in meta.items() if k.lower().startswith("x-amz-meta-")},
            )

    def get(self, bucket: str, key: str) -> Optional[StoredObject]:
        if bucket != self.bucket:
            return None
        return self.objects.get(key)

    def delete(self, bucket: str, key: str) -> bool:
        with self._lock:
            return self.objects.pop(key, None) is not None

    def keys(self) -> list[str]:
        return sorted(self.objects)

    def verbs(self, method: str) -> list[str]:
        return [key for name, key, _ in self.requests if name == method]

    def reset(self) -> None:
        self.requests.clear()
        self.auth_errors.clear()
