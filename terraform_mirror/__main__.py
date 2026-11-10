"""Command line entry point: ``python -m terraform_mirror``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional, Sequence

from .cache import CacheService
from .config import Config, add_server_options, config_from_namespace
from .errors import ProxyError
from .providers import ProviderRef
from .server import build_application, serve
from .storage.base import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terraform-mirror", description="Caching mirror for the Terraform module and provider protocols."
    )
    sub = parser.add_subparsers(dest="command")

    add_server_options(sub.add_parser("serve", help="run the proxy (default command)"))

    add_server_options(sub.add_parser("info", help="print the resolved configuration and exit"))

    prefetch = sub.add_parser("prefetch", help="download module archives into the cache and exit")
    add_server_options(prefetch)
    prefetch.add_argument("modules", nargs="+", help="module addresses, e.g. hashicorp/consul/aws@5.0.0")
    prefetch.add_argument("--force", dest="force", action="store_true",
                          help="re-download and overwrite archives that are already cached")

    cache = sub.add_parser("cache", help="inspect or purge the cache")
    add_server_options(cache)
    cache.add_argument("--list", action="store_true", help="list cached objects (default)")
    cache.add_argument("--purge", action="store_true", help="drop everything known about MODULE")
    cache.add_argument("module", nargs="?", help="namespace/name/provider[@version]")

    health = sub.add_parser("healthcheck", help="probe the storage backends and exit 0 (ok) or 1 (broken)")
    add_server_options(health)
    health.add_argument("--read-only", action="store_true", help="skip the write probe")
    health.add_argument("--pending", action="store_true", help="also print what is still waiting to be downloaded")

    ping = sub.add_parser(
        "ping",
        help="probe this instance' own HTTP endpoint and exit 0 (ok) or 1 (not answering); "
        "scheme, host and port come from TFM_HOST/TFM_PORT/TFM_TLS_CERT",
    )
    ping.add_argument(
        "--url", default=None, help="probe this URL instead of the one derived from the environment"
    )
    ping.add_argument("--ready", action="store_true", help="probe /readyz instead of /healthz")
    ping.add_argument("--timeout", type=float, default=None, help="seconds to wait (default TFM_HEALTH_TIMEOUT)")

    mirror = sub.add_parser(
        "mirror-add",
        help="download provider packages into the mirror cache (warming a cold mirror)",
    )
    add_server_options(mirror)
    mirror.add_argument(
        "providers",
        nargs="+",
        help="host/namespace/type[@version], e.g. registry.terraform.io/hashicorp/vault@3.22.0",
    )
    mirror.add_argument(
        "--platform",
        dest="mirror_platforms",
        action="append",
        default=None,
        help="os_arch to fetch, repeatable (default TFM_PROVIDER_PLATFORMS)",
    )
    mirror.add_argument(
        "--all-versions", dest="all_versions", action="store_true", help="every version the registry lists"
    )
    mirror.add_argument(
        "--force", dest="force", action="store_true", help="re-download packages that are already cached"
    )

    mirror_list = sub.add_parser("mirror-list", help="list the provider packages held in the cache")
    add_server_options(mirror_list)
    return parser


def _split_module(address: str) -> tuple[str, str, str, Optional[str]]:
    """Split ``namespace/name/provider[@version]``.

    Raises ``ValueError`` (not ``SystemExit``) so a bad address in a list of many is
    reported per address instead of aborting the whole command.
    """
    left, at, version = address.partition("@")
    parts = left.split("/")
    if len(parts) != 3:
        raise ValueError(f"module must look like namespace/name/provider[@version], got {address!r}")
    if at and not version:
        raise ValueError(f"empty version after '@' in {address!r}")
    return parts[0], parts[1], parts[2], version or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "serve")
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "ping":
        # Deliberately before the config is built and before an application exists: a
        # liveness probe must not depend on storage settings being valid.
        return cmd_ping(args)

    config = config_from_namespace(args)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if command == "serve":
        return serve(config)

    app, storage, queue = build_application(config)
    try:
        if command == "info":
            print(json.dumps(safe_config(config), indent=2, sort_keys=True))
            return 0
        if command == "healthcheck":
            return cmd_healthcheck(app, args)
        if command == "prefetch":
            return cmd_prefetch(app.cache, args.modules, force=getattr(args, "force", False))
        if command == "mirror-add":
            return cmd_mirror_add(app.providers, args)
        if command == "mirror-list":
            return cmd_mirror_list(app.providers)
        if command == "cache":
            return cmd_cache(app.cache, storage, args)
        parser.error(f"unknown command {command!r}")  # pragma: no cover
        return 2
    finally:
        queue.shutdown(wait=True)


def safe_config(config: Config) -> dict:
    """Configuration summary without any secret material."""
    data = {
        "listen": f"{config.listen_host}:{config.listen_port}",
        "upstream_url": config.upstream_url,
        "external_base_url": config.external_base_url or "(from Host header)",
        "backends": config.backends,
        "cache_dir": config.cache_dir,
        "ttl_versions": config.ttl_versions,
        "ttl_download": config.ttl_download,
        "stale_revalidate": config.stale_revalidate,
        "prefetch_latest": config.prefetch_latest,
        "max_archive_mb": config.max_archive_mb,
        "workers": config.workers,
        "shutdown_grace": config.shutdown_grace,
        "tls": bool(config.tls_cert),
        "admin_token": "set" if config.admin_token else "unset",
        "health": {
            "probe_write": config.health_probe_write,
            "probe_timeout": config.health_timeout,
            "cache_ttl": config.health_cache_ttl,
            "startup_check": config.startup_check,
            "startup_required": config.startup_required,
            "ready_strict": config.ready_strict,
            "ready_upstream": config.ready_upstream,
        },
        "metrics_pending_series": config.metrics_pending_series,
        "provider_mirror": {
            "enabled": config.provider_mirror,
            "upstream_url": config.provider_upstream_url,
            "prefix": f"/{config.provider_mirror_prefix}" if config.provider_mirror_prefix else "(root)",
            "platforms": config.provider_platforms,
            "ttl_versions": config.ttl_provider_versions,
            "block_wait": config.provider_block_wait,
        },
    }
    if config.s3:
        data["s3"] = {
            "bucket": config.s3.bucket,
            "region": config.s3.region,
            "endpoint": config.s3.endpoint_url or "aws",
            "prefix": config.s3.prefix,
            "credentials": "set" if config.s3.access_key else "absent",
            "path_style": config.s3.resolved_path_style(),
        }
    return data


def cmd_healthcheck(app, args) -> int:
    """One-shot version of ``/readyz``: usable as an initContainer or a deploy gate."""
    report = app.health.check(force=True, write=not args.read_only)
    payload = report.to_dict()
    if args.pending:
        payload["pending"] = app.cache.pending.stats()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if report.ok else 1


def cmd_ping(args) -> int:
    """What the image HEALTHCHECK runs: is the HTTP socket answering?

    Unlike ``terraform-mirror healthcheck`` this touches nothing but the local socket, so a broken
    bucket never makes a serving process look dead and it costs the object store nothing.
    """
    from .health import probe_local_endpoint

    check = probe_local_endpoint(url=args.url or "", ready=args.ready, timeout=args.timeout)
    print(f"{'ok' if check.ok else 'fail'} {check.name} {check.detail} ({check.seconds * 1000:.0f} ms)")
    return 0 if check.ok else 1


def cmd_prefetch(cache: CacheService, addresses: list[str], force: bool = False) -> int:
    from .models import ModuleRef
    from .semver import newest, versions_from_payload

    code = 0
    for address in addresses:
        try:
            namespace, name, provider, version = _split_module(address)
            ref = ModuleRef.build(namespace, name, provider, version)
        except (ValueError, ProxyError) as exc:
            print(f"{address}: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
            continue
        try:
            if not version:
                document = cache.versions(ref)
                version = newest(versions_from_payload(document.json))
                if not version:
                    print(f"{ref.module_id}: upstream has no versions", file=sys.stderr)
                    code = 1
                    continue
                ref = ModuleRef.build(namespace, name, provider, version)
            meta = cache.ensure_archive(ref, force=force)
            note = " [forced]" if force else ""
            print(f"{ref.module_id}@{version}: {meta.size} bytes sha256={meta.sha256} [{meta.backend}]{note}")
        except Exception as exc:
            print(f"{ref.module_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
    return code


def cmd_mirror_add(providers, args) -> int:
    """Fetch provider packages into the cache: how a cold mirror gets warm.

    Without ``@version`` the newest version the registry lists is taken; without
    ``--platform`` the configured ``TFM_PROVIDER_PLATFORMS`` are.
    """
    from .semver import newest

    code = 0
    for address in args.providers:
        try:
            ref = ProviderRef.from_address(address)
        except (ProxyError, ValueError) as exc:
            print(f"{address}: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
            continue
        platforms = args.mirror_platforms or providers.platforms
        try:
            if ref.version and not args.all_versions:
                versions = [ref.version]
            else:
                listed = [str(row["version"]) for row in providers.versions(ref).get("versions") or []]
                if not listed:
                    print(f"{ref.provider_id}: the registry lists no versions", file=sys.stderr)
                    code = 1
                    continue
                versions = listed if args.all_versions else [newest(listed)]
        except ProxyError as exc:
            print(f"{ref.provider_id}: {exc.message}", file=sys.stderr)
            code = 1
            continue
        for version in versions:
            for platform in platforms:
                one = ref.with_version(version).with_platform(platform)
                try:
                    meta = providers.ensure(one, force=args.force)
                except ProxyError as exc:
                    print(f"{one}: {exc.message}", file=sys.stderr)
                    code = 1
                    continue
                print(f"{one}: {meta['size']} bytes h1={meta['h1']}")
    return code


def cmd_mirror_list(providers) -> int:
    """One line per cached provider package."""
    rows = providers.list_providers()
    if not rows:
        print("(the provider mirror is empty)", file=sys.stderr)
        return 0
    for row in rows:
        print(
            f"{row.get('provider', '?')}@{row.get('version', '?')}/{row.get('platform', '?')}"
            f"\t{row.get('size', '?')}\th1={row.get('h1', '')}"
        )
    return 0


def cmd_cache(cache: CacheService, storage: Storage, args: argparse.Namespace) -> int:
    from .models import ModuleRef

    if args.module:
        try:
            namespace, name, provider, version = _split_module(args.module)
            ref = ModuleRef.build(namespace, name, provider, version)
        except (ValueError, ProxyError) as exc:
            print(f"{args.module}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        if args.purge:
            print(json.dumps({"purged": cache.purge(ref)}, indent=2))
            return 0
        if version:
            meta = cache.archive_meta(ref)
            if meta is None:
                print(f"{ref.module_id}@{version}: not cached", file=sys.stderr)
                return 1
            print(json.dumps(meta.__dict__, indent=2, default=str))
            return 0
        print(json.dumps({"versions": cache.cached_modules(ref.meta_prefix())}, indent=2))
        return 0

    keys = cache.cached_modules()
    for key in keys:
        meta = storage.stat(key)
        print(f"{meta.size if meta else '?'}\t{key}")
    if not keys:
        print("(cache is empty)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
