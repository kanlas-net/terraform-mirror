"""HTTP client for the upstream Terraform registry and for archive downloads."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import NotFound, UpstreamError
from .models import ModuleRef

#: Header the registry protocol uses to tell Terraform where to fetch the module.
X_TERRAFORM_GET = "X-Terraform-Get"


@dataclass
class UpstreamResponse:
    status: int
    body: bytes
    headers: "Headers" = field(default_factory=lambda: Headers())

    def __post_init__(self) -> None:
        if not isinstance(self.headers, Headers):
            self.headers = Headers(self.headers)

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class Headers(dict):
    """Header mapping with case-insensitive lookup.

    ``urllib`` hands over a case-preserving mapping, and copying it into a plain dict makes
    ``X-Terraform-Get`` depend on how the upstream happened to capitalise it. HTTP header
    names are case-insensitive, so the proxy treats them that way everywhere.
    """

    def __init__(self, items=None) -> None:
        super().__init__()
        for name, value in dict(items or {}).items():
            super().__setitem__(str(name).lower(), value)

    def __setitem__(self, name: str, value: str) -> None:
        super().__setitem__(str(name).lower(), value)

    def __getitem__(self, name: str):
        return super().__getitem__(str(name).lower())

    def get(self, name: str, default=None):
        return super().get(str(name).lower(), default)

    def update(self, other=(), **kwargs) -> None:  # pragma: no cover - defensive
        for name, value in dict(other or {}, **kwargs).items():
            self[name] = value

    def pop(self, name, *default):
        return super().pop(str(name).lower(), *default)

    def __contains__(self, name: object) -> bool:
        return super().__contains__(str(name).lower())


class RegistryClient:
    """Talks to ``registry.terraform.io`` (or any compatible upstream)."""

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float = 10.0,
        user_agent: str = "terraform-mirror",
        opener: Optional[Callable[..., object]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.user_agent = user_agent
        self._opener = opener or urllib.request.urlopen

    # ---- low level ---------------------------------------------------------
    def _open(self, url: str, method: str = "GET", timeout: Optional[float] = None):
        request = urllib.request.Request(url, method=method)
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept", "application/json")
        try:
            return self._opener(request, timeout=timeout or self.request_timeout)
        except urllib.error.HTTPError as exc:
            return exc
        except urllib.error.URLError as exc:
            raise UpstreamError(
                502, f"upstream registry is unreachable: {exc.reason}", reason="unreachable"
            ) from exc

    def request(self, path: str, method: str = "GET") -> UpstreamResponse:
        url = f"{self.base_url}{path}"
        with self._open(url, method) as response:
            body = response.read()
            headers = Headers(response.headers.items())
            status = getattr(response, "status", None) or response.getcode()
        return UpstreamResponse(status=status, body=body, headers=headers)

    # ---- registry protocol -------------------------------------------------
    def list_versions(self, ref: ModuleRef) -> UpstreamResponse:
        path = f"/v1/modules/{ref.namespace}/{ref.name}/{ref.provider}/versions"
        response = self.request(path)
        if response.status == 404:
            raise NotFound(f"module {ref.module_id} is not published upstream", "module_not_found")
        if response.status >= 400:
            raise UpstreamError(
                502,
                f"upstream answered {response.status} for {ref.module_id} version list",
                reason=f"http_{response.status}",
            )
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpstreamError(502, "upstream returned a malformed version list", reason="malformed") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("modules"), list):
            raise UpstreamError(
                502, "upstream version list is missing the 'modules' key", reason="malformed"
            )
        return response

    def download_source(self, ref: ModuleRef) -> UpstreamResponse:
        if not ref.version:
            raise UpstreamError(500, "version required to resolve a download source", reason="config")
        path = f"/v1/modules/{ref.namespace}/{ref.name}/{ref.provider}/{ref.version}/download"
        response = self.request(path, method="GET")
        if response.status == 404:
            raise NotFound(f"module {ref.module_id}@{ref.version} is not published upstream", "module_not_found")
        if response.status >= 400:
            raise UpstreamError(
                502,
                f"upstream answered {response.status} for the download source",
                reason=f"http_{response.status}",
            )
        getter = response.headers.get(X_TERRAFORM_GET)
        if not getter:
            raise UpstreamError(
                502,
                f"upstream did not return {X_TERRAFORM_GET} for {ref.module_id}@{ref.version}",
                reason="missing_header",
            )
        return response

    # ---- health ------------------------------------------------------------
    def probe(self) -> tuple[bool, str, float]:
        """Is the registry reachable? ``(ok, detail, seconds)``.

        Any HTTP answer counts as reachable, a 404 included: what the probe has to rule
        out is DNS, TCP, TLS and certificate trouble, not whether a module exists.
        """
        started = time.perf_counter()
        try:
            response = self._open(f"{self.base_url}/.well-known/terraform.json")
        except UpstreamError as exc:
            return False, str(exc), time.perf_counter() - started
        try:
            status = getattr(response, "status", None) or response.getcode()
        finally:
            try:
                response.close()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover - closing is best effort
                pass
        return True, f"registry answered HTTP {status}", time.perf_counter() - started


class ArchiveDownloader:
    """Streams a remote archive to a temporary file and reports size + sha256."""

    def __init__(
        self,
        *,
        download_timeout: float = 60.0,
        max_bytes: int = 512 * 1024 * 1024,
        user_agent: str = "terraform-mirror",
        opener: Optional[Callable[..., object]] = None,
        tmp_dir: Optional[str] = None,
    ) -> None:
        self.download_timeout = download_timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.tmp_dir = tmp_dir
        self._opener = opener or urllib.request.urlopen

    def fetch(self, url: str, *, expected_sha256: Optional[str] = None) -> "DownloadedArchive":
        scheme, _sep, _rest = url.partition("://")
        if scheme.lower() not in ("http", "https"):
            # Defence in depth: the proxy must never fetch a local or exotic scheme, even
            # if a caller hands it a source that skipped the cacheable check.
            raise UpstreamError(502, f"refusing to fetch a {scheme or 'schemeless'} URL", reason="bad_scheme")
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", self.user_agent)
        try:
            response = self._opener(request, timeout=self.download_timeout)
        except urllib.error.HTTPError as exc:
            raise UpstreamError(502, f"archive source answered HTTP {exc.code}", reason=f"http_{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(502, f"archive source is unreachable: {exc.reason}", reason="unreachable") from exc

        fd, path = tempfile.mkstemp(prefix="tfp-dl-", dir=self.tmp_dir, suffix=".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with response, os.fdopen(fd, "wb") as out:
                status = getattr(response, "status", None) or response.getcode()
                if status >= 400:
                    raise UpstreamError(502, f"archive source answered HTTP {status}", reason=f"http_{status}")
                content_type = response.headers.get("Content-Type", "")
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UpstreamError(
                            502, f"archive exceeds the {self.max_bytes} byte limit", reason="too_large"
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise UpstreamError(502, "archive source returned an empty body", reason="empty")
            sha256 = digest.hexdigest()
            if expected_sha256 and expected_sha256.lower() != sha256:
                raise UpstreamError(
                    502, "downloaded archive does not match the expected checksum", reason="checksum"
                )
            return DownloadedArchive(path=path, size=size, sha256=sha256, content_type=content_type)
        except BaseException:
            _unlink(path)
            raise


@dataclass
class DownloadedArchive:
    path: str
    size: int
    sha256: str
    content_type: str = "application/zip"

    def move_into(self, directory: str, filename: str) -> str:
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, filename)
        shutil.move(self.path, target)
        return target

    def cleanup(self) -> None:
        _unlink(self.path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
