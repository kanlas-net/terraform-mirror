# Changelog

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning is
[SemVer](https://semver.org/). A release is marked by a git tag `vX.Y.Z`, which is also what
publishes the image to `ghcr.io`.

## [Unreleased]

## [1.0.0] - 2026-11-10

First release. A caching mirror for the two protocols the Terraform CLI reads from a registry, in
one image, on the Python standard library only: the Python package is `terraform_mirror`, the
command is `terraform-mirror` and every setting starts with `TFM_*`.

### Added

- **Module registry protocol v1**, in full: `GET /.well-known/terraform.json`,
  `/v1/modules/<host>/<ns>/<name>/<prov>/versions`, `…/<version>/download` and `/archive/…zip`, so
  `terraform init` works through the mirror unchanged. Documents are cached on a TTL and revalidated
  in the background (`stale-while-revalidate`), and a mirror that has the bytes keeps serving them
  while the upstream is away (`stale-on-error`).
- **Background archive download with deduplication**: the client gets `204` + `X-Terraform-Get`
  immediately, the archive is fetched once by a worker. A non-`http` source (`git::…`) is proxied
  through as is and not cached.
- **Provider network mirror**: `index.json`, `<version>.json` and the provider zip under the
  `/:host/:ns/:type/` layout Terraform expects, with `h1:` and `zh:` hashes computed from what is
  actually served (an upstream archive is repacked when needed so the hashes describe the file we
  return). `terraform-mirror mirror-add` / `mirror-list` maintain the mirror list.
- **Backends**: a local directory (`fs`), an S3-compatible bucket (`s3`), or both (`fs,s3` — read
  fs then s3, write both, warm the disk on an S3 hit). The S3 V4 signature is hand-written on the
  standard library (`terraform_mirror/sigv4.py`) and cross-checked against `botocore` vectors.
- **Coordination between instances**: lock files in the storage with a TTL and take-over of an
  expired lock, so one archive is downloaded once even with several instances on one bucket.
- **Graceful shutdown**: `SIGTERM`/`SIGINT` drains in-flight work for `TFM_SHUTDOWN_GRACE` and
  releases every lock the process holds.
- **Observability**: `/healthz` (liveness), `/readyz` (storage read/write/delete probe), `/metrics`
  (Prometheus text format) and the `terraform-mirror healthcheck` / `terraform-mirror ping` CLI for container health
  checks. Every access-log line carries the response size and `cache=hit|miss|stale|stale-on-error`,
  the same word appears in the `X-TFM-Cache` header, and `terraform_mirror_cache_events_total{kind,status}`
  aggregates it over time.
- **Administration** behind `TFM_ADMIN_TOKEN`: `/api/v1/cache` and `/api/v1/prefetch`, the latter
  with `{"force": true}` to re-fetch an object that is already cached.
- **HTTPS directly** via `TFM_TLS_CERT`/`TFM_TLS_KEY`, no reverse proxy required.
- CI (tests on Python 3.10–3.13, image build, smoke test) and multi-arch images
  (`linux/amd64`, `linux/arm64`) published to the GitHub Container Registry on the `vX.Y.Z` tag.

The runtime is the Python standard library only — no FastAPI, no boto3.

### Known limitations

- `Range` requests for archives are not supported, an archive is served in full.
- Unfinished background downloads do not survive a process restart; the next request retries them.
