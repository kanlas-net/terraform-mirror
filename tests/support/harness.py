"""Shared test scaffolding: build a proxy wired to the local fakes."""

from __future__ import annotations

import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from terraform_mirror.config import Config, S3Config
from terraform_mirror.server import ProxyApplication, ProxyServer, build_application, make_server
from terraform_mirror.storage.base import Storage
from terraform_mirror.workers import BackgroundQueue


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        import json

        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> Optional[str]:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class ProxyHarness:
    """Owns a temp cache directory, the application and (optionally) a live socket."""

    def __init__(self, config: Config, test: object) -> None:
        self.config = config
        self.test = test
        self.app: ProxyApplication
        self.storage: Storage
        self.queue: BackgroundQueue
        self.app, self.storage, self.queue = build_application(config)
        self._server: Optional[ProxyServer] = None
        self._tempdir: Optional[str] = None

    # ---- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        test: object,
        registry_url: str,
        *,
        backends=("fs",),
        s3_endpoint: Optional[str] = None,
        s3_bucket: str = "tf-cache",
        s3_prefix: str = "terraform-mirror",
        s3_access_key: Optional[str] = "AKITEST",
        s3_secret_key: Optional[str] = "secret-test",
        **overrides,
    ) -> "ProxyHarness":
        tempdir = tempfile.mkdtemp(prefix="tfp-test-")
        kwargs = dict(
            listen_host="127.0.0.1",
            listen_port=0,
            upstream_url=registry_url,
            external_base_url="",
            backends=list(backends),
            cache_dir=tempdir,
            ttl_versions=overrides.pop("ttl_versions", 3600),
            ttl_download=overrides.pop("ttl_download", 3600),
            stale_revalidate=overrides.pop("stale_revalidate", True),
            prefetch_latest=overrides.pop("prefetch_latest", True),
            max_archive_mb=overrides.pop("max_archive_mb", 8),
            workers=overrides.pop("workers", 2),
            admin_token=overrides.pop("admin_token", ""),
            locks_enabled=overrides.pop("locks_enabled", True),
            lock_ttl=overrides.pop("lock_ttl", 60),
            lock_wait=overrides.pop("lock_wait", 2),
            log_level="WARNING",
        )
        if "s3" in kwargs["backends"]:
            kwargs["s3"] = S3Config(
                bucket=s3_bucket,
                region="ru-central1",
                endpoint_url=s3_endpoint,
                access_key=s3_access_key,
                secret_key=s3_secret_key,
                prefix=s3_prefix,
                path_style=True,
            )
        kwargs.update(overrides)
        harness = cls(Config(**kwargs).validate(), test)
        harness._tempdir = tempdir
        return harness

    @property
    def cache(self):
        return self.app.cache

    # ---- live server -------------------------------------------------------
    def start_server(self) -> "ProxyHarness":
        self._server = make_server(self.config, self.app)
        self._server.serve_in_thread()
        self.config.listen_port = self._server.port
        return self

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise AssertionError("start the server first (harness.start_server())")
        return f"http://127.0.0.1:{self._server.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.queue.shutdown(wait=True, timeout=3.0)
        if self._tempdir:
            shutil.rmtree(self._tempdir, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------
    def wait_idle(self, timeout: float = 15.0) -> bool:
        return self.queue.wait_idle(timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[bytes] = None,
        base: Optional[str] = None,
    ) -> Response:
        url = (base or self.base_url) + path
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                status = response.getcode()
                hdrs = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            hdrs = dict(exc.headers.items())
        return Response(status=status, headers=hdrs, body=raw)

    def get(self, path: str, **kwargs) -> Response:
        return self.request("GET", path, **kwargs)

    def head(self, path: str, **kwargs) -> Response:
        return self.request("HEAD", path, **kwargs)

    def post(self, path: str, body: bytes = b"", **kwargs) -> Response:
        return self.request("POST", path, body=body, **kwargs)

    def delete(self, path: str, **kwargs) -> Response:
        return self.request("DELETE", path, **kwargs)

    # ---- in-process shortcut (no socket) ----------------------------------
    def handle(self, method: str, path: str, **kwargs) -> Response:
        parts = urllib.parse.urlsplit(path)
        response = self.app.handle(method, parts.path, parts.query, kwargs.pop("headers", None), kwargs.pop("body", b""))
        body = response.body if response.body is not None else b"".join(response.stream or [])
        return Response(status=response.status, headers=dict(response.headers), body=body)
