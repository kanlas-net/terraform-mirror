"""Configuration: command line arguments and ``TFM_*`` environment variables."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .errors import ConfigError

_BACKEND_RE = re.compile(r"^(fs|s3)$")
DEFAULT_UPSTREAM = "https://registry.terraform.io"
#: The provider mirror reads from the public registry unless told otherwise.
DEFAULT_PROVIDER_UPSTREAM = "https://registry.terraform.io"
#: What almost every consumer of a provider mirror needs, and nothing else by default:
#: a mirror that fetches all twelve platforms on first touch would fetch a gigabyte.
DEFAULT_PROVIDER_PLATFORM = "linux_amd64"
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z][a-z0-9]*$")


def _env_list(name: str, default: str) -> list[str]:
    """A comma separated list setting; empty entries are dropped, order is kept."""
    raw = _env(name, default) or default
    seen: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen or [default]


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    # A typo like TFM_LOCKS=flase must not silently switch the feature off.
    raise ConfigError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off), got {raw!r}")


@dataclass
class S3Config:
    bucket: str
    region: str = "us-east-1"
    endpoint_url: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    session_token: Optional[str] = None
    prefix: str = "terraform-mirror"
    #: ``None`` = auto: path style for custom endpoints, virtual-host style for AWS.
    path_style: Optional[bool] = None
    #: Use ``UNSIGNED-PAYLOAD`` instead of hashing the body (only valid over HTTPS).
    unsigned_payload: bool = False
    #: Send ``If-None-Match: *`` when creating lock objects (AWS, MinIO, R2 support it).
    conditional_locks: bool = False
    #: Second read-back delay used by the lock create handshake when conditional
    #: writes are not available.
    lock_verify_delay: float = 0.25

    def resolved_path_style(self) -> bool:
        if self.path_style is not None:
            return self.path_style
        if not self.endpoint_url:
            return False
        host = _host_of(self.endpoint_url)
        return not host.endswith("amazonaws.com")

    def validate(self) -> None:
        if not self.bucket:
            raise ConfigError("S3 backend selected but no bucket configured (TFM_S3_BUCKET)")
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,62}$", self.bucket):
            raise ConfigError(f"invalid S3 bucket name: {self.bucket!r}")
        if not self.region:
            raise ConfigError("S3 backend selected but no region configured (TFM_S3_REGION)")
        if self.endpoint_url and not self.endpoint_url.startswith(("http://", "https://")):
            raise ConfigError("TFM_S3_ENDPOINT must start with http:// or https://")
        if bool(self.access_key) != bool(self.secret_key):
            raise ConfigError("S3 credentials must come as a pair (access key + secret key)")


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    #: Base URL of the upstream registry (no trailing slash).
    upstream_url: str = DEFAULT_UPSTREAM
    #: Public base URL of *this* proxy; used to build absolute X-Terraform-Get URLs.
    #: When empty it is derived from the request ``Host`` header.
    external_base_url: str = ""

    backends: list[str] = field(default_factory=lambda: ["fs"])
    cache_dir: str = "./cache"

    #: Seconds a version list may be served from cache before revalidation.
    ttl_versions: int = 3600
    #: Seconds a resolved download source may be cached (versions are immutable).
    ttl_download: int = 604800
    #: Serve a stale cached document while refreshing it in the background.
    stale_revalidate: bool = True

    #: Background prefetch when a client asks for the version list.
    prefetch_latest: bool = True

    max_archive_mb: int = 512
    #: Socket timeout for the small registry JSON calls (versions, download).
    request_timeout: float = 10.0
    #: Socket timeout while streaming an archive (per socket read, not total).
    download_timeout: float = 60.0
    user_agent: str = "terraform-mirror"

    workers: int = 4
    queue_max: int = 2000
    #: Seconds the shutdown hook gets for in-flight downloads before the process exits.
    #: Keep it below the Kubernetes ``terminationGracePeriodSeconds``.
    shutdown_grace: int = 15
    #: Copy objects fetched from S3 into the local disk cache on the fly.
    promote_to_fs: bool = True

    #: Distributed download locks (needed when several instances share one bucket).
    locks_enabled: bool = True
    #: Seconds before a lock left behind by a killed process may be broken.
    lock_ttl: int = 900
    #: Seconds a blocking request waits for another instance to finish a download.
    lock_wait: int = 120

    admin_token: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    log_level: str = "INFO"

    # ---- health / readiness ------------------------------------------------
    #: Include a write round trip (put, read back, delete) in the storage probes.
    health_probe_write: bool = True
    #: Seconds a readiness result is reused, so kubelet polling does not hammer the bucket.
    health_cache_ttl: float = 5.0
    #: Socket timeout used by probes, seconds. Much shorter than a normal object operation.
    health_timeout: float = 5.0
    #: Probe the storage backends once before serving traffic.
    startup_check: bool = True
    #: How many times to retry the startup probe (a bucket mid-restart gets a chance).
    startup_check_retries: int = 5
    startup_check_interval: float = 3.0
    #: Exit non-zero instead of serving when the startup probe fails.
    startup_required: bool = False
    #: False = one healthy backend is enough to be ready (degraded but serving).
    ready_strict: bool = True
    #: Let a broken upstream registry decide readiness too.
    ready_upstream: bool = False

    # ---- metrics -----------------------------------------------------------
    #: Per-module pending series to export (0 = totals only). Bounds cardinality.
    metrics_pending_series: int = 50

    # ---- provider network mirror -------------------------------------------
    #: Serve the provider network mirror protocol at all.
    provider_mirror: bool = True
    #: Registry the mirror reads versions and packages from.
    provider_upstream_url: str = DEFAULT_PROVIDER_UPSTREAM
    #: Path prefix the mirror endpoints live under; empty means the root, which is
    #: what a public mirror uses and keeps us a drop-in replacement for one.
    provider_mirror_prefix: str = ""
    #: Seconds a provider version list is served from cache before revalidation.
    ttl_provider_versions: int = 3600
    #: Which ``os_arch`` packages to advertise and fetch. A mirror may host a subset.
    provider_platforms: list[str] = field(default_factory=lambda: [DEFAULT_PROVIDER_PLATFORM])
    #: Seconds a mirror request waits for a package that is not cached yet.
    provider_block_wait: float = 120.0

    s3: Optional[S3Config] = None

    # ---- derived -----------------------------------------------------------
    @property
    def max_archive_bytes(self) -> int:
        return self.max_archive_mb * 1024 * 1024

    def validate(self) -> "Config":
        if self.ttl_versions <= 0 or self.ttl_download <= 0:
            raise ConfigError("TTLs must be positive")
        if not 0 <= self.listen_port <= 65535:
            raise ConfigError("listen port out of range (0 means 'pick a free port')")
        if self.workers < 1:
            raise ConfigError("at least one background worker is required")
        if self.max_archive_mb < 1:
            raise ConfigError("max archive size must be at least 1 MiB")
        if not self.backends:
            raise ConfigError("at least one storage backend is required")
        unknown = [b for b in self.backends if not _BACKEND_RE.match(b)]
        if unknown:
            raise ConfigError(f"unknown storage backend(s): {', '.join(unknown)}")
        if len(set(self.backends)) != len(self.backends):
            raise ConfigError("storage backends must be unique")
        if "s3" in self.backends:
            if self.s3 is None:
                raise ConfigError("backend 's3' selected but S3 is not configured")
            self.s3.validate()
        if self.external_base_url and not self.external_base_url.startswith(("http://", "https://")):
            raise ConfigError("external base URL must start with http:// or https://")
        if self.lock_ttl <= 0:
            raise ConfigError("lock TTL must be positive")
        if self.lock_wait < 0:
            raise ConfigError("lock wait must be zero or positive")
        if self.shutdown_grace < 0:
            raise ConfigError("the shutdown grace period cannot be negative")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ConfigError("TLS needs both a certificate and a key")
        if self.health_cache_ttl < 0:
            raise ConfigError("health cache TTL must be zero or positive")
        if self.health_timeout <= 0:
            raise ConfigError("health probe timeout must be positive")
        if self.startup_check_retries < 1:
            raise ConfigError("the startup check needs at least one attempt")
        if self.startup_check_interval < 0:
            raise ConfigError("the startup check interval must be zero or positive")
        if self.metrics_pending_series < 0:
            raise ConfigError("the pending metrics limit cannot be negative")
        if self.ttl_provider_versions <= 0:
            raise ConfigError("the provider version TTL must be positive")
        if self.provider_block_wait < 0:
            raise ConfigError("the provider block wait cannot be negative")
        if not self.provider_platforms:
            raise ConfigError("at least one provider platform is required")
        bad = [item for item in self.provider_platforms if not _PLATFORM_RE.fullmatch(str(item))]
        if bad:
            raise ConfigError(f"provider platforms must look like os_arch, got {', '.join(map(str, bad))}")
        prefix = str(self.provider_mirror_prefix or "").strip("/")
        if ".." in prefix or any(part in ("", ".") for part in prefix.split("/") if prefix):
            raise ConfigError("the mirror prefix must be a plain path segment, e.g. mirror")
        self.provider_mirror_prefix = prefix
        self.provider_upstream_url = str(self.provider_upstream_url or DEFAULT_PROVIDER_UPSTREAM).rstrip("/")
        return self

    def public_base(self, request_host: str = "", scheme: str = "http") -> str:
        """Base URL used in generated ``X-Terraform-Get`` values."""
        if self.external_base_url:
            return self.external_base_url.rstrip("/")
        if request_host:
            return f"{scheme}://{request_host}"
        host = self.listen_host if self.listen_host != "0.0.0.0" else "127.0.0.1"
        return f"{'https' if self.tls_cert else scheme}://{host}:{self.listen_port}"

    # ---- factories ---------------------------------------------------------
    @classmethod
    def from_env(cls, environ: Optional[dict[str, str]] = None) -> "Config":
        """Build a config from the environment.

        With an explicit *environ* the process environment is swapped for its lifetime;
        that is meant for the single-threaded startup path and tests, not for hot reload
        while workers are running.
        """
        if environ is not None:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(environ)
            try:
                return cls._from_env()
            finally:
                os.environ.clear()
                os.environ.update(saved)
        return cls._from_env()

    @classmethod
    def _from_env(cls) -> "Config":
        backends = [b.strip() for b in (_env("TFM_BACKENDS", "fs") or "fs").split(",") if b.strip()]
        s3: Optional[S3Config] = None
        if "s3" in backends:
            s3 = S3Config(
                bucket=_env("TFM_S3_BUCKET", "") or "",
                region=_env("TFM_S3_REGION", "us-east-1") or "us-east-1",
                endpoint_url=_env("TFM_S3_ENDPOINT"),
                access_key=_env("TFM_S3_ACCESS_KEY") or _env("AWS_ACCESS_KEY_ID"),
                secret_key=_env("TFM_S3_SECRET_KEY") or _env("AWS_SECRET_ACCESS_KEY"),
                session_token=_env("TFM_S3_SESSION_TOKEN") or _env("AWS_SESSION_TOKEN"),
                prefix=(_env("TFM_S3_PREFIX", "terraform-mirror") or "terraform-mirror").strip("/"),
                path_style=_env_bool("TFM_S3_PATH_STYLE", False) if _env("TFM_S3_PATH_STYLE") else None,
                unsigned_payload=_env_bool("TFM_S3_UNSIGNED_PAYLOAD", False),
                conditional_locks=_env_bool("TFM_S3_CONDITIONAL_LOCKS", False),
                lock_verify_delay=float(_env_int("TFM_S3_LOCK_VERIFY_MS", 250)) / 1000.0,
            )
        return cls(
            listen_host=_env("TFM_HOST", "0.0.0.0") or "0.0.0.0",
            listen_port=_env_int("TFM_PORT", 8080),
            upstream_url=(_env("TFM_UPSTREAM_URL", DEFAULT_UPSTREAM) or DEFAULT_UPSTREAM).rstrip("/"),
            external_base_url=(_env("TFM_EXTERNAL_URL", "") or "").rstrip("/"),
            backends=backends,
            cache_dir=_env("TFM_CACHE_DIR", "./cache") or "./cache",
            ttl_versions=_env_int("TFM_TTL_VERSIONS", 3600),
            ttl_download=_env_int("TFM_TTL_DOWNLOAD", 604800),
            stale_revalidate=_env_bool("TFM_STALE_REVALIDATE", True),
            prefetch_latest=_env_bool("TFM_PREFETCH_LATEST", True),
            max_archive_mb=_env_int("TFM_MAX_ARCHIVE_MB", 512),
            request_timeout=float(_env_int("TFM_REQUEST_TIMEOUT", 10)),
            download_timeout=float(_env_int("TFM_DOWNLOAD_TIMEOUT", 60)),
            workers=_env_int("TFM_WORKERS", 4),
            queue_max=_env_int("TFM_QUEUE_MAX", 2000),
            shutdown_grace=_env_int("TFM_SHUTDOWN_GRACE", 15),
            promote_to_fs=_env_bool("TFM_PROMOTE_TO_FS", True),
            locks_enabled=_env_bool("TFM_LOCKS", True),
            lock_ttl=_env_int("TFM_LOCK_TTL", 900),
            lock_wait=_env_int("TFM_LOCK_WAIT", 120),
            admin_token=_env("TFM_ADMIN_TOKEN", "") or "",
            tls_cert=_env("TFM_TLS_CERT", "") or "",
            tls_key=_env("TFM_TLS_KEY", "") or "",
            log_level=_env("TFM_LOG_LEVEL", "INFO") or "INFO",
            health_probe_write=_env_bool("TFM_HEALTH_PROBE_WRITE", True),
            health_cache_ttl=_env_float("TFM_HEALTH_CACHE_TTL", 5.0),
            health_timeout=_env_float("TFM_HEALTH_TIMEOUT", 5.0),
            startup_check=_env_bool("TFM_STARTUP_CHECK", True),
            startup_check_retries=_env_int("TFM_STARTUP_CHECK_RETRIES", 5),
            startup_check_interval=_env_float("TFM_STARTUP_CHECK_INTERVAL", 3.0),
            startup_required=_env_bool("TFM_STARTUP_REQUIRED", False),
            ready_strict=_env_bool("TFM_READY_STRICT", True),
            ready_upstream=_env_bool("TFM_READY_UPSTREAM", False),
            metrics_pending_series=_env_int("TFM_METRICS_PENDING_SERIES", 50),
            provider_mirror=_env_bool("TFM_PROVIDER_MIRROR", True),
            provider_upstream_url=(_env("TFM_PROVIDER_UPSTREAM_URL", DEFAULT_PROVIDER_UPSTREAM) or DEFAULT_PROVIDER_UPSTREAM).rstrip("/"),
            provider_mirror_prefix=(_env("TFM_PROVIDER_MIRROR_PREFIX", "") or "").strip("/"),
            ttl_provider_versions=_env_int("TFM_TTL_PROVIDER_VERSIONS", 3600),
            provider_platforms=_env_list("TFM_PROVIDER_PLATFORMS", DEFAULT_PROVIDER_PLATFORM),
            provider_block_wait=float(_env_int("TFM_PROVIDER_BLOCK_WAIT", 120)),
            s3=s3,
        ).validate()

    @classmethod
    def from_args(cls, argv: Optional[Sequence[str]] = None) -> "Config":
        parser = build_arg_parser()
        return config_from_namespace(parser.parse_args(argv))


def add_server_options(parser: argparse.ArgumentParser) -> None:
    """Add every server option to *parser* (shared by the CLI and its subcommands)."""
    parser.add_argument("--host", dest="listen_host", default=None, help="bind address (TFM_HOST)")
    parser.add_argument("--port", dest="listen_port", type=int, default=None, help="bind port (TFM_PORT)")
    parser.add_argument("--upstream", dest="upstream_url", default=None,
                        help="upstream registry base URL (TFM_UPSTREAM_URL)")
    parser.add_argument("--external-url", dest="external_base_url", default=None,
                        help="public base URL of this proxy (TFM_EXTERNAL_URL)")
    parser.add_argument("--backend", default=None, help="comma separated backends: fs, s3 or fs,s3 (TFM_BACKENDS)")
    parser.add_argument("--cache-dir", dest="cache_dir", default=None, help="directory for the filesystem backend")
    parser.add_argument("--ttl-versions", dest="ttl_versions", type=int, default=None,
                        help="version list TTL in seconds (TFM_TTL_VERSIONS)")
    parser.add_argument("--ttl-download", dest="ttl_download", type=int, default=None,
                        help="download metadata TTL in seconds (TFM_TTL_DOWNLOAD)")
    parser.add_argument("--no-stale-revalidate", dest="no_stale_revalidate", action="store_true",
                        help="never answer from a stale cache entry")
    parser.add_argument("--no-prefetch", dest="no_prefetch", action="store_true",
                        help="do not prefetch archives when a client lists versions")
    parser.add_argument("--max-archive-mb", dest="max_archive_mb", type=int, default=None,
                        help="hard size limit per archive (TFM_MAX_ARCHIVE_MB)")
    parser.add_argument("--request-timeout", dest="request_timeout", type=float, default=None,
                        help="socket timeout for registry JSON calls (TFM_REQUEST_TIMEOUT)")
    parser.add_argument("--download-timeout", dest="download_timeout", type=float, default=None,
                        help="socket timeout while streaming an archive (TFM_DOWNLOAD_TIMEOUT)")
    parser.add_argument("--workers", dest="workers", type=int, default=None, help="background download workers")
    parser.add_argument("--queue-max", dest="queue_max", type=int, default=None, help="background queue size")
    parser.add_argument("--shutdown-grace", dest="shutdown_grace", type=int, default=None,
                        help="seconds to let in-flight downloads finish on SIGTERM/SIGINT (TFM_SHUTDOWN_GRACE)")
    parser.add_argument("--lock-ttl", dest="lock_ttl", type=int, default=None,
                        help="seconds before a stale download lock may be broken (TFM_LOCK_TTL)")
    parser.add_argument("--lock-wait", dest="lock_wait", type=int, default=None,
                        help="seconds to wait for another instance to finish a download (TFM_LOCK_WAIT)")
    parser.add_argument("--no-locks", dest="no_locks", action="store_true",
                        help="disable distributed download locks (single instance only)")
    parser.add_argument("--admin-token", dest="admin_token", default=None,
                        help="token required by /api/v1/* (TFM_ADMIN_TOKEN)")
    parser.add_argument("--tls-cert", dest="tls_cert", default=None, help="PEM certificate for HTTPS")
    parser.add_argument("--tls-key", dest="tls_key", default=None, help="PEM key for HTTPS")
    parser.add_argument("--log-level", dest="log_level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--s3-bucket", dest="s3_bucket", default=None)
    parser.add_argument("--s3-region", dest="s3_region", default=None)
    parser.add_argument("--s3-endpoint", dest="s3_endpoint", default=None)
    parser.add_argument("--s3-access-key", dest="s3_access_key", default=None)
    parser.add_argument("--s3-secret-key", dest="s3_secret_key", default=None)
    parser.add_argument("--s3-prefix", dest="s3_prefix", default=None)
    parser.add_argument("--s3-path-style", dest="s3_path_style", action="store_true")
    parser.add_argument("--health-timeout", dest="health_timeout", type=float, default=None,
                        help="socket timeout for storage probes (TFM_HEALTH_TIMEOUT)")
    parser.add_argument("--health-cache-ttl", dest="health_cache_ttl", type=float, default=None,
                        help="seconds a readiness result is reused (TFM_HEALTH_CACHE_TTL)")
    parser.add_argument("--no-health-probe-write", dest="no_health_probe_write", action="store_true",
                        help="do not write a probe object when checking storage (TFM_HEALTH_PROBE_WRITE)")
    parser.add_argument("--no-startup-check", dest="no_startup_check", action="store_true",
                        help="skip the storage probe at startup (TFM_STARTUP_CHECK)")
    parser.add_argument("--startup-check-retries", dest="startup_check_retries", type=int, default=None,
                        help="startup probe attempts (TFM_STARTUP_CHECK_RETRIES)")
    parser.add_argument("--startup-check-interval", dest="startup_check_interval", type=float, default=None,
                        help="seconds between startup probe attempts (TFM_STARTUP_CHECK_INTERVAL)")
    parser.add_argument("--startup-required", dest="startup_required", action="store_true",
                        help="exit non-zero when the startup probe fails (TFM_STARTUP_REQUIRED)")
    parser.add_argument("--ready-permissive", dest="ready_permissive", action="store_true",
                        help="one healthy backend is enough for /readyz (TFM_READY_STRICT)")
    parser.add_argument("--ready-upstream", dest="ready_upstream", action="store_true",
                        help="let a broken upstream registry fail /readyz (TFM_READY_UPSTREAM)")
    parser.add_argument("--metrics-pending-series", dest="metrics_pending_series", type=int, default=None,
                        help="per-module pending series to export, 0 disables them (TFM_METRICS_PENDING_SERIES)")
    parser.add_argument("--no-provider-mirror", dest="no_provider_mirror", action="store_true",
                        help="do not serve the provider network mirror protocol (TFM_PROVIDER_MIRROR)")
    parser.add_argument("--provider-upstream-url", dest="provider_upstream_url", default=None,
                        help="registry the provider mirror reads from (TFM_PROVIDER_UPSTREAM_URL)")
    parser.add_argument("--provider-mirror-prefix", dest="provider_mirror_prefix", default=None,
                        help="serve the mirror under this path prefix instead of the root (TFM_PROVIDER_MIRROR_PREFIX)")
    parser.add_argument("--ttl-provider-versions", dest="ttl_provider_versions", type=int, default=None,
                        help="seconds a provider version list is cached (TFM_TTL_PROVIDER_VERSIONS)")
    parser.add_argument("--provider-platforms", dest="provider_platforms", default=None,
                        help="comma separated os_arch packages to serve (TFM_PROVIDER_PLATFORMS)")
    parser.add_argument("--provider-block-wait", dest="provider_block_wait", type=float, default=None,
                        help="seconds a mirror request waits for a cold package (TFM_PROVIDER_BLOCK_WAIT)")


_SIMPLE_ATTRS = (
    "listen_host",
    "listen_port",
    "upstream_url",
    "external_base_url",
    "cache_dir",
    "ttl_versions",
    "ttl_download",
    "max_archive_mb",
    "request_timeout",
    "download_timeout",
    "workers",
    "queue_max",
    "shutdown_grace",
    "admin_token",
    "lock_ttl",
    "lock_wait",
    "tls_cert",
    "tls_key",
    "log_level",
    "health_cache_ttl",
    "health_timeout",
    "startup_check_retries",
    "startup_check_interval",
    "metrics_pending_series",
    "provider_upstream_url",
    "ttl_provider_versions",
    "provider_block_wait",
)


def config_from_namespace(namespace: argparse.Namespace, base: Optional[Config] = None) -> Config:
    """Build a :class:`Config` from parsed CLI options on top of the environment."""
    cfg = base or Config.from_env()
    for attr in _SIMPLE_ATTRS:
        value = getattr(namespace, attr, None)
        if value is not None:
            setattr(cfg, attr, value)
    backend = getattr(namespace, "backend", None)
    if backend:
        cfg.backends = [b.strip() for b in backend.split(",") if b.strip()]
    if getattr(namespace, "no_stale_revalidate", False):
        cfg.stale_revalidate = False
    if getattr(namespace, "no_prefetch", False):
        cfg.prefetch_latest = False
    if getattr(namespace, "no_locks", False):
        cfg.locks_enabled = False
    if getattr(namespace, "no_health_probe_write", False):
        cfg.health_probe_write = False
    if getattr(namespace, "no_startup_check", False):
        cfg.startup_check = False
    if getattr(namespace, "startup_required", False):
        cfg.startup_required = True
    if getattr(namespace, "ready_permissive", False):
        cfg.ready_strict = False
    if getattr(namespace, "ready_upstream", False):
        cfg.ready_upstream = True
    if getattr(namespace, "no_provider_mirror", False):
        cfg.provider_mirror = False
    prefix = getattr(namespace, "provider_mirror_prefix", None)
    if prefix is not None:
        cfg.provider_mirror_prefix = str(prefix).strip("/")
    platforms = getattr(namespace, "provider_platforms", None)
    if platforms:
        cfg.provider_platforms = [item.strip() for item in platforms.split(",") if item.strip()]
    if "s3" in cfg.backends:
        s3 = cfg.s3 or S3Config(bucket="")
        for dest, attr in (
            ("s3_bucket", "bucket"),
            ("s3_region", "region"),
            ("s3_endpoint", "endpoint_url"),
            ("s3_access_key", "access_key"),
            ("s3_secret_key", "secret_key"),
            ("s3_prefix", "prefix"),
        ):
            value = getattr(namespace, dest, None)
            if value is not None:
                setattr(s3, attr, value)
        if getattr(namespace, "s3_path_style", False):
            s3.path_style = True
        cfg.s3 = s3
    cfg.upstream_url = cfg.upstream_url.rstrip("/")
    cfg.external_base_url = cfg.external_base_url.rstrip("/")
    if cfg.s3 is not None:
        cfg.s3.prefix = str(cfg.s3.prefix or "").strip("/")
    return cfg.validate()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terraform_mirror",
        description="Caching reverse proxy for the Terraform module registry protocol.",
    )
    add_server_options(parser)
    return parser
