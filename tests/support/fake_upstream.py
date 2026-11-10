"""An in-process fake of ``registry.terraform.io`` plus the archive host."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from terraform_mirror.upstream import X_TERRAFORM_GET

DEFAULT_ARCHIVE = b"PK\x03\x04" + b"terraform module fixture payload " * 64


@dataclass
class FakeModule:
    namespace: str
    name: str
    provider: str
    versions: list[str] = field(default_factory=lambda: ["1.0.0", "1.1.0"])
    archive: bytes = DEFAULT_ARCHIVE
    #: ``http`` (cacheable) or ``git`` (must be passed through untouched).
    style: str = "http"
    #: Special failure modes used by the tests.
    archive_status: int = 200
    archive_delay: float = 0.0
    archive_repeat: int = 1
    subdir: str = ""
    download_status: int = 204

    @property
    def module_id(self) -> str:
        return f"{self.namespace}/{self.name}/{self.provider}"

    def versions_payload(self) -> dict:
        return {
            "modules": [
                {
                    "version": version,
                    "providers": [{"name": self.provider, "versions": [">= 1.0"]}],
                }
                for version in self.versions
            ]
        }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- plumbing ----------------------------------------------------------
    @property
    def fake(self) -> "FakeRegistry":
        return self.server.fake  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:  # keep the test output clean
        return None

    def _send(self, status: int, body: bytes = b"", headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and status != 204:
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, {"Content-Type": "application/json"})

    def _not_found(self) -> None:
        self._json({"errors": [{"code": "module_not_found", "message": "not found"}]}, status=404)

    # ---- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = urllib.parse.parse_qs(parts.query)
        self.fake.record(path)

        if path == "/.well-known/terraform.json":
            return self._json({"modules.v1": "/v1/modules/"})

        if path.startswith("/archives/"):
            return self._archive(path.rsplit("/", 1)[-1])

        pieces = [p for p in path.split("/") if p]
        # v1 modules <ns> <name> <provider> [version] [versions|download]
        if len(pieces) >= 6 and pieces[0] == "v1" and pieces[1] == "modules":
            namespace, name, provider = pieces[2], pieces[3], pieces[4]
            module = self.fake.find(namespace, name, provider)
            if module is None:
                return self._not_found()
            if pieces[-1] == "versions":
                return self._json(module.versions_payload())
            if len(pieces) == 7 and pieces[-1] == "download":
                version = pieces[5]
                if version not in module.versions:
                    return self._not_found()
                if module.style == "noheader":
                    return self._send(200, b"")
                if module.style == "git":
                    getter = f"git::https://example.com/{name}.git?ref=v{version}"
                else:
                    subdir = f"//{module.subdir}" if module.subdir else ""
                    getter = (
                        f"{self.fake.base_url}/archives/{namespace}-{name}-{provider}-{version}.zip"
                        f"{subdir}?ref=v{version}"
                    )
                return self._send(module.download_status, b"", {X_TERRAFORM_GET: getter})
        self._not_found()

    def _archive(self, filename: str) -> None:
        module = self.fake.find_by_archive(filename)
        if module is None:
            return self._not_found()
        self.fake.hits["archive"] += 1
        if module.archive_delay:
            time.sleep(module.archive_delay)
        if module.archive_status >= 400:
            return self._send(module.archive_status, b'{"errors":[{"code":"boom","message":"boom"}]}')
        body = module.archive * max(1, module.archive_repeat)
        self._send(200, body, {"Content-Type": "application/zip"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeRegistry:
    """Start with ``.start()``; read ``.hits`` to assert what the proxy asked for."""

    def __init__(self) -> None:
        self.modules: list[FakeModule] = []
        self.hits: dict[str, int] = {"versions": 0, "download": 0, "archive": 0, "other": 0}
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-registry", daemon=True)
        self.port = self._server.server_address[1]

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeRegistry":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- fixtures ----------------------------------------------------------
    def record(self, path: str) -> None:
        with self._lock:
            if path.endswith("/versions"):
                self.hits["versions"] += 1
            elif path.endswith("/download"):
                self.hits["download"] += 1
            elif not path.startswith("/archives/"):
                self.hits["other"] += 1

    def add_module(self, module: FakeModule) -> FakeModule:
        self.modules.append(module)
        return module

    def add(self, name: str, **kwargs) -> FakeModule:
        module = FakeModule(namespace=kwargs.pop("namespace", "hashicorp"), name=name,
                            provider=kwargs.pop("provider", "aws"), **kwargs)
        return self.add_module(module)

    def find(self, namespace: str, name: str, provider: str) -> Optional[FakeModule]:
        for module in self.modules:
            if (module.namespace, module.name, module.provider) == (namespace, name, provider):
                return module
        return None

    def find_by_archive(self, filename: str) -> Optional[FakeModule]:
        for module in self.modules:
            for version in module.versions:
                if filename == f"{module.namespace}-{module.name}-{module.provider}-{version}.zip":
                    return module
        return None

    def reset_hits(self) -> None:
        with self._lock:
            for key in self.hits:
                self.hits[key] = 0
