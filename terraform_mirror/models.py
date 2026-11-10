"""Value objects: module references, storage keys and go-getter source parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .errors import BadRequest

# Terraform registry ids: lowercase alphanumerics plus - _ . (no leading dot, no '..').
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_GETTER_RE = re.compile(r"^(?:(?P<getter>[A-Za-z][A-Za-z0-9]*)::)?(?P<rest>.*)$")
_SUBDIR_RE = re.compile(r"(?<!:)//")


def check_id(value: str, what: str) -> str:
    """Validate a single path component coming from a URL."""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or ".." in value:
        raise BadRequest(f"invalid {what}: {value!r}")
    return value


def check_version(value: str) -> str:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value) or ".." in value:
        raise BadRequest(f"invalid version: {value!r}")
    return value


@dataclass(frozen=True)
class ModuleRef:
    """A module address, optionally pinned to one version."""

    namespace: str
    name: str
    provider: str
    version: Optional[str] = None

    @classmethod
    def build(cls, namespace: str, name: str, provider: str, version: Optional[str] = None) -> "ModuleRef":
        return cls(
            namespace=check_id(namespace, "namespace"),
            name=check_id(name, "name"),
            provider=check_id(provider, "provider"),
            version=check_version(version) if version else None,
        )

    @property
    def module_id(self) -> str:
        return f"{self.namespace}/{self.name}/{self.provider}"

    def __str__(self) -> str:  # pragma: no cover - debugging helper
        return self.module_id if not self.version else f"{self.module_id}@{self.version}"

    # ---- storage keys -------------------------------------------------------
    def archive_key(self) -> str:
        if not self.version:
            raise BadRequest("version required")
        base = f"modules/{self.namespace}/{self.name}/{self.provider}"
        return f"{base}/{self.version}.zip"

    def versions_meta_key(self) -> str:
        return f"meta/versions/{self.namespace}/{self.name}/{self.provider}.json"

    def download_meta_key(self) -> str:
        if not self.version:
            raise BadRequest("version required")
        return f"meta/download/{self.namespace}/{self.name}/{self.provider}/{self.version}.json"

    def meta_prefix(self) -> str:
        return f"meta/versions/{self.namespace}/{self.name}/{self.provider}"


@dataclass(frozen=True)
class GetterSource:
    """Parsed ``X-Terraform-Get`` value (go-getter syntax ``getter::url//subdir?query``)."""

    getter: str
    url: str
    subdir: str = ""
    query: str = ""
    raw: str = ""
    extra: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def fetch_url(self) -> str:
        return f"{self.url}?{self.query}" if self.query else self.url

    @property
    def cacheable(self) -> bool:
        """Only plain HTTP(S) archives can be mirrored to our storage."""
        if self.getter not in ("", "http", "https"):
            return False
        # Scheme case is not significant (RFC 3986), so compare case-insensitively.
        scheme, sep, _ = self.url.partition("://")
        return sep == "://" and scheme.lower() in ("http", "https")

    @property
    def looks_like_archive(self) -> bool:
        path = self.url.lower()
        return path.endswith((".zip", ".tar.gz", ".tgz", ".tar", ".gz", ".bz2", ".xz"))


def parse_getter_source(raw: str) -> GetterSource:
    """Parse an ``X-Terraform-Get`` header value.

    Examples::

        https://github.com/a/b/archive/v1.zip
        git::https://github.com/a/b.git?ref=v1
        https://example.com/a.zip//modules/vpc?ref=v1
    """
    if not raw:
        raise BadRequest("empty X-Terraform-Get")
    match = _GETTER_RE.match(raw.strip())
    if not match:  # pragma: no cover - the regex matches everything
        raise BadRequest(f"cannot parse X-Terraform-Get: {raw!r}")
    getter = (match.group("getter") or "").lower()
    rest = match.group("rest")

    # go-getter documents the subdir before the query ("url//subdir?query"), but
    # hand written registries also emit it after the query ("url?query//subdir").
    # Split whichever marker comes first, then look for the other one in the tail.
    subdir = ""
    query = ""
    sub_match = _SUBDIR_RE.search(rest)
    qidx = rest.find("?")
    if sub_match and (qidx < 0 or sub_match.start() < qidx):
        rest, tail = rest[: sub_match.start()], rest[sub_match.end() :]
        subdir, _, query = tail.partition("?")
    elif qidx >= 0:
        rest, tail = rest[:qidx], rest[qidx + 1 :]
        inner = _SUBDIR_RE.search(tail)
        if inner:
            query, subdir = tail[: inner.start()], tail[inner.end() :]
        else:
            query = tail

    extra: dict[str, str] = {}
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        extra[key] = value

    return GetterSource(getter=getter, url=rest, subdir=subdir, query=query, raw=raw.strip(), extra=extra)
