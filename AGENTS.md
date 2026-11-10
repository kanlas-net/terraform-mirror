# AGENTS.md

Instructions for coding agents working in this repository. Read this before you change
anything; most rules below are enforced by a test, so "it works locally" is not enough.

`terraform-mirror` is a caching mirror for the Terraform **module** registry protocol (registry
protocol v1) and for the **provider network mirror** protocol, written against the Python
standard library only. The Python package is `terraform_mirror`, the command is `terraform-mirror`
and every setting starts with `TFM_*`.

## Commands

```bash
# the whole suite (~35 s)
python3 -m unittest discover -s tests -t .

# one file / one test
python3 -m unittest tests.test_locking -v
python3 -m unittest tests.test_cache.VersionsTest.test_stale_is_served_and_refreshed_in_background

# run the proxy
python3 -m terraform_mirror serve --port 8080 --backend fs --cache-dir .tf-cache

# the suite inside the image (what CI runs)
docker build --target test -t terraform-mirror-test . && docker run --rm terraform-mirror-test
```

`skipped=4` is the expected result, not a failure: those four are the SigV4 cross-check against
`botocore`, which is an optional extra (`pip install .[dev]` runs them). Two more skip without
`cryptography` (also in `[dev]`): the TLS probe tests mint a throwaway certificate with it, so a
bare-stdlib run reports `skipped=6` and a full `.[dev]` run reports none. Inside the image the
bare count is `skipped=10` — the four workflow tests skip themselves because `.github/` is not in
the build context. CI installs both extras so everything runs.

Ruff is configured in `pyproject.toml` (line length 118, rules `E,F,W,I,UP,B,C4,SIM`, ignoring
`E501, SIM105, B904`) but is **not** installed by default. Do not claim lint passed if you never
ran it.

## Hard constraints

1. **Standard library only at runtime.** `dependencies = []` in `pyproject.toml` is deliberate,
   and `ProjectMetadataTest.test_no_third_party_imports_in_the_package` walks every file in
   `terraform_mirror/` and fails on a non-stdlib import. Adding a dependency is a design decision, not a
   convenience — the S3 V4 signature exists as hand-written code (`terraform_mirror/sigv4.py`) for exactly
   this reason.
2. **Python >= 3.10.** CI runs 3.10, 3.11, 3.12 and 3.13. No syntax that only works on 3.11+.
3. **The version lives in two places**: `terraform_mirror.__version__` and `version` in `pyproject.toml`.
   A test asserts they match.
4. **Every `TFM_*` setting must be documented.** `ReadmeTest.test_every_setting_is_documented`
   extracts the names from `terraform_mirror/config.py` and fails if one is missing from the README table.
5. **`.dockerignore` must keep `tests/`, `README.md`, `pyproject.toml` and `docker-compose.yml`
   in the build context.** The `test` stage does `COPY . /src` and runs the suite, so excluding
   them silently breaks the image build. `DockerfileTest.test_dockerignore_keeps_everything_the_suite_needs`
   guards this.
6. **CI must never get `packages: write`.** Only `release.yml` publishes, and a test asserts
   `ci.yml` does not contain that permission.
7. **Any job whose script calls `gh` or `git` must run `actions/checkout` first.** Both died with
   `failed to run git: fatal: not a git repository` once. Guarded by
   `test_jobs_that_shell_out_to_git_or_gh_check_the_tree_out`.
8. **Tag and branch filters in workflows are globs, not regular expressions.** `v*.*.*` is correct;
   the matcher is checked against real tag names in
   `test_release_tag_filter_actually_matches_the_version_tag`.
9. **The repository is English-only** — code, comments, docs and test data. Do not add text in
   another language.
10. **A re-pushed tag must publish the release again.** Deleting a tag turns its release into a
   draft instead of deleting it, and `gh release view` still finds drafts, so a step that only
   checks existence skips publishing forever. Guarded by
   `test_a_leftover_draft_release_is_published_not_skipped`.
11. **Liveness never touches the network.** `/healthz` stays a local check, so an S3 outage cannot
   restart every pod. Anything that probes storage belongs on `/readyz` (and in
   `terraform-mirror healthcheck`), and its result is cached for `TFM_HEALTH_CACHE_TTL` because kubelet polls
   far more often than a bucket should be LISTed. See `tests.test_health`.
12. **The image `HEALTHCHECK` runs `terraform-mirror ping`, never a literal URL.** `make_server` wraps the
    one socket in TLS when `TFM_TLS_CERT` is set, so a hardcoded `http://127.0.0.1:8080/healthz`
    reads "unhealthy" forever on a TLS instance (and on any other `TFM_PORT`); `ping` derives
    scheme/host/port from the environment instead and does not verify the certificate. `ping` also
    must not build a `Config` or an application — a liveness probe that needs valid storage
    settings reports a dead process whenever the bucket config is wrong. Guarded by
    `DockerfileTest.test_healthcheck_uses_the_env_aware_probe` and `tests.test_health.PingCommandTest`.
13. **Metric labels come from a bounded set** (route, op, result, reason, backend). A module id
   only ever reaches a label through `terraform_mirror_archive_pending_age_seconds`, which is capped by
   `TFM_METRICS_PENDING_SERIES` and forgets the series that disappeared. Error *messages* are never
   labels — that is what `UpstreamError.reason` is for.

## Naming trap

`cache.metrics` is the Prometheus bundle; the counter snapshot served by `/api/v1/cache` is
`cache.counters()`. They used to share the name, and a `Metrics object is not callable` is that
clash reappearing.

## Layout

| Path | Responsibility |
|---|---|
| `terraform_mirror/server.py` | HTTP layer: routing on the **raw** path, registry protocol responses, stdlib server |
| `terraform_mirror/cache.py` | TTL document cache, stale-while-revalidate, `stale-on-error` |
| `terraform_mirror/providers.py` | the provider network mirror: refs, `h1`/`zh` hashes, archive flattening, the registry client, the provider cache |
| `terraform_mirror/workers.py` | background queue: the "answer now, download later" half |
| `terraform_mirror/locking.py` | distributed download locks (`LockManager`), handed back on shutdown via `release_all` |
| `terraform_mirror/health.py` | liveness vs readiness, the storage/upstream probes, the startup retry loop, the `terraform-mirror ping` socket probe |
| `terraform_mirror/metrics.py` | hand-written Prometheus registry and the `Metrics` bundle (every exported series) |
| `terraform_mirror/pending.py` | ledger of "a client asked for it, storage does not have it yet" |
| `terraform_mirror/storage/` | `base` interface (+ probes), `fs`, `s3`, `multi` (tiered: read first hit, write all) |
| `terraform_mirror/upstream.py` | HTTP client for the registry and for archive downloads |
| `terraform_mirror/sigv4.py` | hand-written AWS Signature V4 |
| `terraform_mirror/models.py` | module refs, storage keys, go-getter source parsing |
| `terraform_mirror/config.py` | CLI flags and `TFM_*` environment variables |
| `tests/support/` | `fake_upstream.py`, `fake_provider_registry.py`, `fake_s3.py`, `harness.py` |

## Testing conventions

- The fakes are **real HTTP servers** on real sockets, not mocks. `fake_s3.py` actually validates
  `Authorization`, `x-amz-content-sha256` and `If-None-Match`, so a wrong signature fails there
  rather than being asserted directly.
- `ProxyHarness.create()` sets `prefetch_latest=True` by default. A test that purges or inspects
  the cache must call `self.wait()` after `cache.versions(...)`, otherwise a background prefetch
  lands mid-assertion.
- A job still queued when the harness shuts down stays in the queue. Assert
  `stats()["inflight"] == 0` instead of `wait_idle()` in that case.
- Do not add `time.sleep()`. Use the harness wait helpers; `wait_idle(timeout=...)` already polls.
- Cross-instance behaviour has its own test (`test_server_registry.py::SharedS3Test`) — two proxy
  instances, one bucket. Extend it rather than testing locking only in isolation.
  Note that two `fs`-only harnesses do **not** share a lock: each owns its own cache directory, so a
  "the peer could not take the lock" assertion needs the S3 tier.
- `ProxyHarness.get()`/`post()` prepend the proxy's own base URL: pass a **path**
  (`/registry.terraform.io/hashicorp/consul/index.json`), never a full URL, or the request goes to a
  nonsense host.
- The drain order (stop accepting → drain in-flight downloads → release held locks) is covered by
  `tests/test_shutdown.py` through `server.shutdown_application`, because `serve()` itself only
  reacts to signals in the main thread and cannot be driven from a test.

## Do not regress

Each of these was a real bug found in review; the surrounding tests exist because of it.

- `storage/s3.py`: `_request` calls `rewind(data)` before **every** attempt and skips the retry
  when `replayable(data)` is false. A body that cannot be rewound must not be retried.
- `storage/fs.py`: a non-numeric `fetched_at` in the sidecar falls back to the file `mtime`; the
  file is fsynced before `os.replace`.
- `upstream.Headers` is a case-insensitive dict — `X-Terraform-Get` must not depend on how the
  upstream capitalised it.
- `server.py` routes on the **raw** request path and `unquote`s each captured segment afterwards.
  Decoding before routing lets `%2F` escape a route.
- `ArchiveDownloader.fetch` refuses any non-`http(s)` URL; `GetterSource.cacheable` compares the
  scheme case-insensitively.
- `LockManager.refresh` restores `handle.expires_at` when it fails; a failed refresh must not
  leave a lock looking permanently owned.
- `LockManager.release_all` deletes only locks whose token is ours, and a delete that *failed* keeps
  the lock tracked so the next attempt can retry. `NullLockManager` must keep the `_held`
  bookkeeping because it inherits `stats()` — without it `/healthz` answers 500.
- `workers.shutdown(timeout=...)` is one shared deadline for every worker, not one per thread; a
  Kubernetes grace period is a single budget for the whole process.
- `workers.start()` clears `_stop`, so a queue can be restarted.
- `StreamHandle.chunks()` raises `ValueError` after close.
- `config._env_bool` raises `ConfigError` on a value that is not a boolean.
- `storage/s3.py`: `probe()` uses `probe_timeout`, not the 60 s object timeout — a readiness probe
  must not hold a kubelet check for a minute.
- `metrics.py`: a pending limit of `0` means "no per-module series", not "unlimited", and gauges
  built from a snapshot must `forget()` the label combinations that vanished, or a module cached an
  hour ago keeps reporting an age forever.
- `_Handler._send` must not call `send_response()`: the stdlib logs the request inside it, before
  anything about the response is known, so the access line could carry neither the size nor the
  cache status. It calls `send_response_only()` and re-sends `Server`/`Date` itself — dropping
  those two silently removes them from every response.
- `providers.py`: a package description is stored under `….info.json`, **not** `….meta.json` — the
  filesystem backend names its own sidecars that way, so a `.meta.json` key both disappears from
  `storage.list()` (which skips sidecars) and overwrites the sidecar of the zip itself.
  `tests.test_providers` asserts both halves.
- `providers.py`: `BackgroundQueue.submit(key, func, *args, **kwargs)` forwards keyword arguments,
  and `ProviderCacheService.ensure` takes `wait` as **keyword-only** — a background job must be
  submitted with `wait=0.0`, or the job dies with a `TypeError` inside the worker thread.
- `providers.py`: `zipfile.ZipFile.writestr()` has no `mode` argument. Preserving the executable bit
  while flattening an archive needs a `ZipInfo` with `external_attr` copied over.
- `providers.py`: `LockManager` has no "disabled" flag — `NullLockManager` is the disabled one, and a
  default for the provider cache's own locks must use that, not a keyword that does not exist.

## Releasing

```bash
# 1. bump terraform_mirror/__init__.py and pyproject.toml together, and give CHANGELOG.md a
#    "## [X.Y.Z]" heading (a test looks for exactly that string)
git commit -am "release 1.2.0"
git push origin main

# 2. tag it and push the tag as its own ref
git tag -a v1.2.0 -m "release 1.2.0"
git push origin refs/tags/v1.2.0
```

- **`git tag v1.2.0` + `git push --follow-tags` publishes nothing.** A lightweight tag is not
  carried by `--follow-tags`, so the tag never reaches GitHub and `release.yml` never starts. Push
  the tag explicitly (`refs/tags/v1.2.0`), and push it to the Gitea mirror separately too.

- **A workflow file must already exist on the default branch before a tag can trigger it.**
  Pushing a new `release.yml` and its tag in the same `git push` silently publishes nothing.
  If you change a workflow, push the branch first, then tag.
- The tag must equal `terraform_mirror.__version__`, or the `verify` job fails before anything is pushed.
- **Re-tagging a version is allowed.** Delete the tag, push the new one; the release job publishes
  the draft the old tag left behind. Check the release page afterwards — a draft is invisible there.
  Moving the tag is also what you do when CI failed: the `verify` job runs the whole unit-test suite
  **before** anything is published, so a red run means nothing was pushed to ghcr and no release
  exists — fix the test, push `main`, then delete and re-create the tag on the new commit.
  (Done for real on 1.2.0: a shutdown test raced the download it was observing, four CI pythons
  failed, the fix moved `v1.2.0` from `33d7074` to `58091a1`.)
- **The release body is thin by design of the workflow** — `--generate-notes` plus one line with the
  image URL. The prose lives in `CHANGELOG.md`; after publishing, put that version's section into the
  release body (`gh release edit <tag> --notes-file …`, or `PATCH /repos/<o>/<r>/releases/<id>`),
  otherwise the release page says nothing about what changed.
- Semver as used here: **MAJOR** breaks the client or the config (protocol path, renamed or
  re-purposed `TFM_*` variable, bucket key format); **MINOR** is backwards compatible (endpoint,
  variable, backend); **PATCH** is fixes only.

## Before you finish

- Run the full suite; `python3 -m unittest discover -s tests -t .` must end `OK`.
- If you touched `Dockerfile`, `.dockerignore`, `pyproject.toml` or `.github/workflows/`, also
  build the test stage: `docker build --target test . && docker run --rm terraform-mirror-test`.
- If you added or renamed a setting, update the README table in the same commit.
- Never report a build, publish or deploy as successful without a check that proves it (HTTP
  status, command output, a live request).
