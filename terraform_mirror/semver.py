"""Small semver helper used to pick the newest version of a module."""

from __future__ import annotations

import re
from typing import Iterable

_SEMVER_RE = re.compile(
    r"^[vV]?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def _pre_key(pre: str | None) -> tuple:
    """Semver precedence: a version with a pre-release sorts *before* the release."""
    if not pre:
        return (1,)
    parts: list = [0]
    for identifier in pre.split("."):
        if identifier.isdigit():
            parts.append((0, int(identifier), ""))
        else:
            parts.append((1, 0, identifier))
    return tuple(parts)


def version_key(version: str) -> tuple:
    match = _SEMVER_RE.match(version or "")
    if not match:
        return (-1, -1, -1, (0,), version)
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch") or 0)
    return (major, minor, patch, _pre_key(match.group("pre")), "")


def sort_versions(versions: Iterable[str]) -> list[str]:
    """Sort ascending (oldest first); non-semver values sort before every release."""
    return sorted(versions, key=version_key)


def newest(versions: Iterable[str]) -> str | None:
    ordered = sort_versions(versions)
    return ordered[-1] if ordered else None


def versions_from_payload(payload) -> list[str]:
    """Extract the version strings from a registry ``/versions`` document."""
    out: list[str] = []
    if not isinstance(payload, dict):
        return out
    for entry in payload.get("modules") or []:
        if isinstance(entry, dict):
            version = entry.get("version")
            if isinstance(version, str) and version:
                out.append(version)
    return out
