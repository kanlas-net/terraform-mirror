"""Provider network mirror: serve Terraform providers out of our own storage.

This is a second, unrelated protocol to the module registry one. Terraform CLI
speaks it when the CLI config says ``provider_installation { network_mirror { url } }``,
and the three endpoints it calls (measured against a live public mirror, not only
read out of the documentation) are::

    GET <base>/:hostname/:namespace/:type/index.json
        -> {"versions": {"5.13.0": {}, "5.12.0": {}}}
    GET <base>/:hostname/:namespace/:type/:version.json
        -> {"archives": {"linux_amd64": {"url": "terraform-provider-vault_5.13.0_linux_amd64.zip",
                                        "hashes": ["h1:…", "zh:…"]}}}
    GET <base>/:hostname/:namespace/:type/<that url>
        -> the zip, plugin binary at the archive root

The archive URL is resolved relative to the ``:version.json`` URL, so we keep the
flat layout the public mirror uses: the zip sits next to ``index.json``. That makes
this proxy a drop-in replacement for an existing mirror.

``hashes`` are optional in the protocol and the public mirror omits them, which
leaves every installed binary unverified. Both hash schemes Terraform understands
are computable without any secret (:func:`package_hashes`), so we do serve them.

A 404 on ``index.json`` is how a mirror says "I do not have this provider"; any
other status is a protocol error to Terraform, so upstream failures are reported
as 502 rather than as "not here".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from .errors import BadRequest, NotFound, ProxyError, UpstreamError
from .locking import LockManager, NullLockManager
from .models import check_id, check_version
from .storage.base import Storage, hash_file
from .upstream import ArchiveDownloader, RegistryClient

PROVIDERS_PREFIX = "providers/"
#: Every provider release asset starts with this; it is how we recognise the plugin.
PLUGIN_PREFIX = "terraform-provider-"
DEFAULT_HOST = "registry.terraform.io"
DEFAULT_PLATFORM = "linux_amd64"

_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$")
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z][a-z0-9]*$")
#: ``terraform-provider-<type>_<version>_<os>_<arch>.zip``; parsed from the right,
#: because neither the type nor the version may contain an underscore but both may
#: contain dots and hyphens.
_ARCHIVE_NAME_RE = re.compile(r"^terraform-provider-.+_(?P<version>[^_]+)_(?P<platform>[a-z0-9]+_[a-z0-9]+)\.zip$")


def check_hostname(value: str) -> str:
    """Validate the registry hostname segment of a provider address."""
    host = (value or "").strip().lower().rstrip(".")
    if not _HOSTNAME_RE.fullmatch(host):
        raise BadRequest(f"invalid registry hostname: {value!r}")
    return host


@dataclass(frozen=True)
class ProviderRef:
    """A provider address, optionally pinned to a version and a platform."""

    host: str = DEFAULT_HOST
    namespace: str = ""
    type: str = ""
    version: str = ""
    platform: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", check_hostname(self.host))
        object.__setattr__(self, "namespace", check_id(self.namespace, "namespace"))
        object.__setattr__(self, "type", check_id(self.type, "type"))
        if self.version:
            object.__setattr__(self, "version", check_version(self.version))
        if self.platform and not _PLATFORM_RE.fullmatch(self.platform):
            raise BadRequest(f"platform must look like os_arch, got {self.platform!r}")

    @classmethod
    def from_address(cls, address: str) -> "ProviderRef":
        """Parse ``host/namespace/type[@version]``; the host may be left out."""
        left, at, version = (address or "").strip().partition("@")
        parts = [part for part in left.strip("/").split("/") if part]
        if len(parts) == 2:
            parts = [DEFAULT_HOST, *parts]
        if len(parts) != 3:
            raise BadRequest(
                f"provider must look like host/namespace/type[@version], got {address!r}"
            )
        if at and not version:
            raise BadRequest(f"empty version after '@' in {address!r}")
        return cls(host=parts[0], namespace=parts[1], type=parts[2], version=version)

    def with_version(self, version: str) -> "ProviderRef":
        return ProviderRef(self.host, self.namespace, self.type, version, self.platform)

    def with_platform(self, platform: str) -> "ProviderRef":
        return ProviderRef(self.host, self.namespace, self.type, self.version, platform)

    @property
    def provider_id(self) -> str:
        return f"{self.host}/{self.namespace}/{self.type}"

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        suffix = f"@{self.version}" if self.version else ""
        suffix += f"/{self.platform}" if self.platform else ""
        return f"{self.provider_id}{suffix}"

    # ---- storage keys ------------------------------------------------------
    @property
    def base_prefix(self) -> str:
        return f"{PROVIDERS_PREFIX}{self.host}/{self.namespace}/{self.type}/"

    @property
    def versions_key(self) -> str:
        return f"{self.base_prefix}versions.json"

    def archive_name(self, platform: Optional[str] = None) -> str:
        """The file name we serve, matching what public mirrors use."""
        plat = platform or self.platform
        if not plat:
            raise BadRequest("a platform is required to name a provider archive")
        if not self.version:
            raise BadRequest("a version is required to name a provider archive")
        return f"{PLUGIN_PREFIX}{self.type}_{self.version}_{plat}.zip"

    def archive_key(self, platform: Optional[str] = None) -> str:
        return f"{self.base_prefix}{self.archive_name(platform)}"

    def meta_key(self, platform: Optional[str] = None) -> str:
        """Where the description of one package lives.

        Not ``.meta.json``: the filesystem backend names its own sidecars that way and
        ``list()`` skips them, so a key with that suffix would both hide from listing and
        overwrite the sidecar of the zip itself.
        """
        return f"{self.base_prefix}{self.archive_name(platform)}.info.json"

    @staticmethod
    def platform_from_name(filename: str) -> tuple[str, str]:
        """``(version, platform)`` out of an archive file name, or ``("", "")``."""
        match = _ARCHIVE_NAME_RE.fullmatch(filename or "")
        if not match:
            return "", ""
        return match.group("version"), match.group("platform")


# ---- hashes --------------------------------------------------------------


def package_hashes(path: str) -> tuple[str, str]:
    """Return ``(h1, zh)`` for a provider zip.

    ``h1:`` reproduces ``golang.org/x/mod/sumdb/dirhash.Hash1`` the way Terraform
    computes it for an archive: sha256 of the concatenation of
    ``"<sha256(file)>  <path>\\n"`` lines over every file entry sorted by path,
    base64-encoded. ``zh:`` is the legacy scheme: base64 of the sha256 of the zip.

    Neither needs a key, which is why a mirror can offer verified packages even for
    archives it had to repack.
    """
    outer = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            outer.update(chunk)
    inner = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            digest = hashlib.sha256()
            with archive.open(info) as member:
                while True:
                    chunk = member.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            inner.update(f"{digest.hexdigest()}  {info.filename}\n".encode())
    return (
        "h1:" + base64.b64encode(inner.digest()).decode(),
        "zh:" + base64.b64encode(outer.digest()).decode(),
    )


def normalize_package(path: str) -> tuple[str, bool]:
    """Make the plugin binary sit at the archive root; returns ``(path, repacked)``.

    HashiCorp release assets already do (``LICENSE.txt`` + the binary). A GitHub
    archive — what ``github://owner/repo:ref.zip?subdir=dist`` resolves to — nests
    everything under ``repo-ref/``, so we repack the shallowest directory that
    directly contains a ``terraform-provider-*`` file. The caller keeps ownership of
    neither path: on repack the original file is removed and the new one lives beside it.
    """
    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        roots = sorted(
            {
                name.rsplit("/", 1)[0]
                for name in names
                if "/" in name and name.rsplit("/", 1)[1].startswith(PLUGIN_PREFIX)
            },
            key=lambda value: (len(value), value),
        )
        if not roots and any(name.startswith(PLUGIN_PREFIX) for name in names):
            return path, False  # already flat, nothing to do
        if not roots:
            raise UpstreamError(
                502,
                "the provider archive contains no terraform-provider-* file",
                reason="bad_archive",
            )
        prefix = roots[0] + "/"
        members = [name for name in names if name.startswith(prefix)]
        if not members:  # pragma: no cover - defensive, roots came from names
            raise UpstreamError(502, "provider archive layout is not understood", reason="bad_archive")
        fd, packed = tempfile.mkstemp(prefix="tfp-provider-", suffix=".zip", dir=os.path.dirname(path) or ".")
        os.close(fd)
        try:
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as out:
                for name in members:
                    info = source.getinfo(name)
                    # Keep the mode: a provider binary has to stay executable inside the
                    # archive, and ZipInfo is the only way writestr takes one.
                    target = zipfile.ZipInfo(name[len(prefix) :], date_time=info.date_time)
                    target.compress_type = zipfile.ZIP_DEFLATED
                    target.external_attr = info.external_attr
                    out.writestr(target, source.read(name))
        except BaseException:
            _unlink(packed)
            raise
    _unlink(path)
    return packed, True


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - best effort cleanup
        pass


# ---- upstream ------------------------------------------------------------


@dataclass
class ProviderDownload:
    """Where one provider package actually lives, after the registry resolved it."""

    url: str
    sha256: str = ""
    filename: str = ""
    source: str = ""
    protocols: list[str] = field(default_factory=list)

    @property
    def cacheable(self) -> bool:
        scheme, sep, _ = self.url.partition("://")
        return sep == "://" and scheme.lower() in ("http", "https")


def resolve_provider_source(raw: str) -> tuple[str, str]:
    """Turn a provider ``X-Terraform-Get`` into ``(https url, subdir)``.

    Two shapes matter: a plain ``https`` archive URL, and ``github://owner/repo:ref.zip``
    which go-getter expands to the GitHub codeload archive — that archive always nests
    its contents under ``repo-ref/``, which :func:`normalize_package` then flattens.
    """
    raw = (raw or "").strip()
    if not raw:
        raise UpstreamError(502, "upstream gave an empty download source", reason="no_source")
    if raw.startswith("github://"):
        body = raw[len("github://") :]
        query = ""
        if "?" in body:
            body, query = body.split("?", 1)
        repo, _, ref = body.partition(":")
        ref = ref.removesuffix(".zip")
        if "/" not in repo or not ref:
            raise UpstreamError(502, f"cannot read the github source {raw!r}", reason="bad_source")
        subdir = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair).get("subdir", "")
        return f"https://github.com/{repo}/archive/{ref}.zip", subdir
    if re.match(r"^https?://", raw):
        return raw, ""
    raise UpstreamError(502, f"unsupported provider source: {raw!r}", reason="bad_source")


class ProviderRegistryClient:
    """Reads the upstream provider registry protocol (v1).

    Two download shapes exist in the wild and both are handled: the JSON body with
    ``download_url`` that registry.terraform.io answers today at
    ``…/<version>/download/<os>/<arch>``, and the documented ``204`` + ``X-Terraform-Get``
    form at ``…/<version>/download?os=&arch=``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float = 10.0,
        user_agent: str = "terraform-mirror",
        opener=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = RegistryClient(
            base_url, request_timeout=request_timeout, user_agent=user_agent, opener=opener
        )

    # ---- versions ----------------------------------------------------------
    def versions(self, ref: ProviderRef) -> list[dict]:
        """``[{"version": "5.13.0", "platforms": ["linux_amd64"], "protocols": ["5.0"]}]``."""
        response = self._client.request(f"/v1/providers/{ref.namespace}/{ref.type}/versions")
        if response.status == 404:
            raise NotFound(f"{ref.provider_id} is not published upstream", "provider_not_found")
        if response.status >= 400:
            raise UpstreamError(
                502,
                f"upstream answered {response.status} for {ref.provider_id} versions",
                reason=f"http_{response.status}",
            )
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpstreamError(502, "upstream returned a malformed version list", reason="malformed") from exc
        rows = []
        for row in payload.get("versions") or []:
            version = str(row.get("version") or "")
            if not version:
                continue
            platforms = [
                f"{item.get('os')}_{item.get('arch')}"
                for item in row.get("platforms") or []
                if item.get("os") and item.get("arch")
            ]
            rows.append(
                {
                    "version": version,
                    "platforms": platforms,
                    "protocols": [str(item) for item in row.get("protocols") or []],
                }
            )
        return rows

    # ---- one package -------------------------------------------------------
    def download(self, ref: ProviderRef) -> ProviderDownload:
        if not ref.version or not ref.platform:
            raise ProxyError(400, "version_and_platform", "a version and a platform are required")
        os_name, arch = ref.platform.split("_", 1)
        stem = f"/v1/providers/{ref.namespace}/{ref.type}/{ref.version}/download"
        response = self._client.request(f"{stem}/{os_name}/{arch}")
        if response.status == 404:
            raise NotFound(
                f"{ref.provider_id}@{ref.version} has no {ref.platform} package upstream", "platform_not_found"
            )
        if response.status < 400:
            body = (response.body or b"").strip()
            if body:
                return self._from_json(ref, body)
            getter = response.headers.get("x-terraform-get") or ""
            if getter:
                return self._from_getter(ref, getter)
            # Some registries only answer the query-string form.
            response = self._client.request(f"{stem}?os={os_name}&arch={arch}")
            if response.status < 400:
                getter = response.headers.get("x-terraform-get") or ""
                if getter:
                    return self._from_getter(ref, getter)
                if (response.body or b"").strip():
                    return self._from_json(ref, response.body)
            raise UpstreamError(502, "upstream gave no download source", reason="no_source")
        raise UpstreamError(
            502, f"upstream answered {response.status} for the download source", reason=f"http_{response.status}"
        )

    @staticmethod
    def _from_json(ref: ProviderRef, body: bytes) -> ProviderDownload:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpstreamError(502, "upstream download metadata is not JSON", reason="malformed") from exc
        url = str(payload.get("download_url") or "")
        if not url:
            raise UpstreamError(502, "upstream download metadata has no download_url", reason="malformed")
        return ProviderDownload(
            url=url,
            sha256=str(payload.get("shasum") or ""),
            filename=str(payload.get("filename") or ""),
            source=f"registry:{ref.provider_id}@{ref.version}",
            protocols=[str(item) for item in payload.get("protocols") or []],
        )

    @staticmethod
    def _from_getter(ref: ProviderRef, raw: str) -> ProviderDownload:
        url, _subdir = resolve_provider_source(raw)
        return ProviderDownload(url=url, source=raw.strip(), filename="")

    def probe(self) -> tuple[bool, str, float]:
        """Is the provider registry reachable? Used by ``/readyz`` optionally."""
        started = time.monotonic()
        try:
            response = self._client.request("/v1/providers/hashicorp/random/versions")
        except ProxyError as exc:
            return False, exc.message, time.monotonic() - started
        ok = response.status < 400
        return ok, f"HTTP {response.status}", time.monotonic() - started


# ---- the cache -----------------------------------------------------------


@dataclass
class ProviderTrace:
    """What one mirror request actually did — for the response header and the access log.

    ``versions`` is what happened to the version list: ``hit`` (fresh in storage), ``miss``
    (nothing cached, fetched now), ``refresh`` (the cached copy had expired) or
    ``stale-on-error`` (the registry failed, the cached list was served anyway). ``packages``
    records the same idea per platform: ``hit`` = already in storage, ``miss`` = fetched now.
    """

    versions: str = ""
    packages: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """One word for the whole request: did we have to go out, or was it all here?"""
        if "miss" in self.packages.values():
            return "miss"
        if self.versions in ("miss", "refresh", "stale-on-error"):
            return self.versions
        if self.packages:
            return "hit"
        return self.versions or "hit"


def _note_versions(trace: Optional[ProviderTrace], status: str) -> None:
    if trace is not None:
        trace.versions = status


def _note_package(trace: Optional[ProviderTrace], platform: str, status: str) -> None:
    if trace is not None:
        trace.packages[platform] = status


class ProviderCacheService:
    """Provider packages: cached in our storage, fetched from the registry on a miss.

    Serving never blocks on the network when the package is already in storage, and a
    version list that is cached but expired is still served when the registry is down:
    a mirror that answers from what it has is more useful than a correct 502.
    """

    def __init__(
        self,
        config,
        storage: Storage,
        client: ProviderRegistryClient,
        downloader: ArchiveDownloader,
        locks: Optional[LockManager] = None,
        queue=None,
        metrics=None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.client = client
        self.downloader = downloader
        self.locks = locks if locks is not None else NullLockManager()
        self.queue = queue
        self.metrics = metrics
        self.logger = logger or logging.getLogger("terraform_mirror.providers")
        self._local: dict[str, threading.Lock] = {}
        self._local_guard = threading.Lock()

    # ---- small helpers -----------------------------------------------------
    def _lock_for(self, key: str) -> threading.Lock:
        with self._local_guard:
            handle = self._local.get(key)
            if handle is None:
                handle = threading.Lock()
                self._local[key] = handle
            return handle

    def _count(self, name: str, value: int = 1) -> None:
        if self.metrics is not None:
            try:
                self.metrics.provider_event(name)
            except AttributeError:  # pragma: no cover - a bare Metrics without the helper
                pass

    @property
    def platforms(self) -> list[str]:
        return list(getattr(self.config, "provider_platforms", [DEFAULT_PLATFORM]) or [DEFAULT_PLATFORM])

    # ---- versions ----------------------------------------------------------
    def versions(self, ref: ProviderRef, *, force: bool = False, trace: Optional[ProviderTrace] = None) -> dict:
        """The cached version list, refreshed when it is older than the TTL."""
        ttl = float(getattr(self.config, "ttl_provider_versions", 3600))
        now = time.time()
        cached = self._read_json(ref.versions_key)
        if not force and cached and (now - float(cached.get("fetched_at") or 0)) < ttl:
            _note_versions(trace, "hit")
            return cached
        try:
            rows = self.client.versions(ref)
        except NotFound:
            if cached := self._read_json(ref.versions_key):
                self.logger.warning("upstream no longer lists %s, serving the cached list", ref.provider_id)
                _note_versions(trace, "stale-on-error")
                return cached
            raise
        except ProxyError as exc:
            cached = cached or self._read_json(ref.versions_key)
            if cached:
                self.logger.warning("upstream failed (%s), serving the cached version list", exc.message)
                _note_versions(trace, "stale-on-error")
                return cached
            raise
        payload = {"fetched_at": now, "provider": ref.provider_id, "versions": rows}
        self.storage.put_bytes(ref.versions_key, json.dumps(payload).encode(), content_type="application/json")
        _note_versions(trace, "refresh" if cached else "miss")
        return payload

    def index_payload(self, ref: ProviderRef, trace: Optional[ProviderTrace] = None) -> dict:
        """The ``index.json`` body: ``{"versions": {"5.13.0": {}, …}}``."""
        rows = self.versions(ref, trace=trace).get("versions") or []
        return {"versions": {str(row["version"]): {} for row in rows}}

    def platforms_for(self, ref: ProviderRef, trace: Optional[ProviderTrace] = None) -> list[str]:
        """Which platforms to advertise for one version: ours ∩ what upstream lists."""
        wanted = set(self.platforms)
        rows = {str(row["version"]): row for row in self.versions(ref, trace=trace).get("versions") or []}
        row = rows.get(ref.version)
        if row is None:
            raise NotFound(f"{ref.provider_id}@{ref.version} is not published upstream", "version_not_found")
        listed = [item for item in row.get("platforms") or [] if item in wanted]
        return listed or list(self.platforms)

    # ---- packages ----------------------------------------------------------
    def meta(self, ref: ProviderRef) -> Optional[dict]:
        """The stored description of one package, or ``None`` when we do not have it."""
        if not ref.version or not ref.platform:
            return None
        return self._read_json(ref.meta_key())

    def ensure(
        self,
        ref: ProviderRef,
        *,
        wait: Optional[float] = None,
        force: bool = False,
        trace: Optional[ProviderTrace] = None,
    ) -> dict:
        """Fetch one package if it is not in storage yet; blocking.

        ``wait`` is how long to wait when another instance holds the lock; ``None``
        means ``TFM_LOCK_WAIT``.
        """
        if not ref.version or not ref.platform:
            raise ProxyError(400, "version_and_platform", "a version and a platform are required")
        key = ref.archive_key()
        if not force:
            existing = self.meta(ref)
            if existing:
                _note_package(trace, ref.platform, "hit")
                return existing
        with self._lock_for(key):
            if not force:
                existing = self.meta(ref)
                if existing:
                    _note_package(trace, ref.platform, "hit")
                    return existing
            timeout = float(getattr(self.config, "lock_wait", 120) if wait is None else wait)
            handle = self.locks.acquire(key)
            if handle is None:
                if self.locks.wait_status(key, timeout) == "timeout":
                    raise ProxyError(
                        503,
                        "download_in_progress",
                        f"another instance is fetching {ref}, retry shortly",
                        {"Retry-After": "5"},
                    )
                meta = self.meta(ref)
                if meta:
                    return meta
                handle = self.locks.acquire(key)
                if handle is None:
                    raise ProxyError(
                        503,
                        "download_in_progress",
                        f"another instance is fetching {ref}, retry shortly",
                        {"Retry-After": "5"},
                    )
            try:
                meta = self._fetch(ref, key)
                _note_package(trace, ref.platform, "miss")
                return meta
            finally:
                self.locks.release(handle)

    def _fetch(self, ref: ProviderRef, key: str) -> dict:
        started = time.perf_counter()
        target = self.client.download(ref)
        if not target.cacheable:
            raise ProxyError(422, "not_cacheable", f"upstream source for {ref} is not an HTTP archive")
        downloaded = self.downloader.fetch(target.url, expected_sha256=target.sha256 or None)
        path, repacked = normalize_package(downloaded.path)
        try:
            h1, zh = package_hashes(path)
            sha256, size = hash_file(path)
            meta = {
                "provider": ref.provider_id,
                "version": ref.version,
                "platform": ref.platform,
                "filename": ref.archive_name(),
                "size": size,
                "sha256": sha256,
                "h1": h1,
                "zh": zh,
                "source": target.source or target.url,
                "repacked": repacked,
                "fetched_at": time.time(),
            }
            self.storage.put_file(
                key, path, size=size, sha256=sha256, content_type="application/zip"
            )
            self.storage.put_bytes(ref.meta_key(), json.dumps(meta).encode(), content_type="application/json")
        finally:
            _unlink(path)
        self._count("provider_stored")
        self.logger.info(
            "cached %s (%s bytes%s) in %.1f s",
            ref,
            size,
            ", repacked to flatten the archive" if repacked else "",
            time.perf_counter() - started,
        )
        return meta

    def schedule(self, ref: ProviderRef) -> bool:
        """Queue a background fetch. Background jobs never wait on a peer's lock."""
        if self.queue is None or not ref.version or not ref.platform:
            return False
        if self.meta(ref):
            return False
        return self.queue.submit(f"provider:{ref}", self.ensure, ref.with_platform(ref.platform), wait=0.0)

    def open_archive(self, ref: ProviderRef):
        return self.storage.open(ref.archive_key())

    def archives_for(
        self, ref: ProviderRef, *, block_wait: Optional[float] = None, trace: Optional[ProviderTrace] = None
    ) -> dict:
        """The ``<version>.json`` body: platforms we can actually serve, with hashes."""
        platforms = self.platforms_for(ref, trace=trace)
        wait = getattr(self.config, "provider_block_wait", 120.0) if block_wait is None else block_wait
        archives: dict[str, dict] = {}
        for platform in platforms:
            one = ref.with_platform(platform)
            meta = self.meta(one)
            if meta is None:
                try:
                    meta = self.ensure(one, wait=wait, trace=trace)
                except ProxyError as exc:
                    self.logger.warning("cannot serve %s: %s", one, exc.message)
                    self._count("provider_failed")
                    continue
            else:
                _note_package(trace, platform, "hit")
            archives[platform] = {
                "url": ref.archive_name(platform),
                "hashes": [item for item in (meta.get("h1"), meta.get("zh")) if item],
            }
        return archives

    # ---- maintenance -------------------------------------------------------
    def list_providers(self) -> list[dict]:
        """Everything in storage, one entry per cached package."""
        out: list[dict] = []
        for key in self.storage.list(PROVIDERS_PREFIX):
            if not key.endswith(".info.json"):
                continue
            meta = self._read_json(key)
            if meta:
                out.append(meta)
        out.sort(key=lambda item: (item.get("provider", ""), item.get("version", ""), item.get("platform", "")))
        return out

    def purge(self, ref: ProviderRef) -> list[str]:
        """Drop everything cached for one provider, or only one version of it."""
        removed: list[str] = []
        for key in self.storage.list(ref.base_prefix):
            if ref.version and ref.version not in key:
                continue
            if self.storage.delete(key):
                removed.append(key)
        return removed

    def _read_json(self, key: str) -> Optional[dict]:
        raw = self.storage.read_or_none(key)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self.logger.warning("ignoring unreadable object %s", key)
            return None
        return payload if isinstance(payload, dict) else None
