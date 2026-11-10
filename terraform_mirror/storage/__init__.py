"""Storage backends: filesystem and S3, behind one small interface."""

from __future__ import annotations

from .base import ObjectMeta, StreamHandle, Storage
from .fs import FilesystemStorage
from .multi import TieredStorage
from .s3 import S3Storage

__all__ = [
    "ObjectMeta",
    "ProbeResult",
    "StreamHandle",
    "Storage",
    "FilesystemStorage",
    "S3Storage",
    "TieredStorage",
    "build_storage",
]


def build_storage(config) -> Storage:
    """Instantiate the configured backends, in lookup order."""
    from ..errors import ConfigError  # local import to avoid a cycle at import time

    backends: list[Storage] = []
    for name in config.backends:
        if name == "fs":
            backends.append(FilesystemStorage(config.cache_dir))
        elif name == "s3":
            if config.s3 is None:
                raise ConfigError("backend 's3' selected but S3 is not configured")
            backends.append(S3Storage(config.s3, probe_timeout=getattr(config, "health_timeout", 5.0)))
        else:  # pragma: no cover - validated in Config.validate()
            raise ConfigError(f"unknown backend {name!r}")
    if len(backends) == 1:
        return backends[0]
    return TieredStorage(backends, promote_first=config.promote_to_fs)
