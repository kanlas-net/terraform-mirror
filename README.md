# terraform-mirror

[![CI](https://github.com/kanlas-net/terraform-mirror/actions/workflows/ci.yml/badge.svg)](https://github.com/kanlas-net/terraform-mirror/actions/workflows/ci.yml)
[![Release](https://github.com/kanlas-net/terraform-mirror/actions/workflows/release.yml/badge.svg)](https://github.com/kanlas-net/terraform-mirror/actions/workflows/release.yml)
[![Version 1.0.0](https://img.shields.io/badge/version-1.0.0-blue)](https://github.com/kanlas-net/terraform-mirror/releases)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-terraform--mirror-2496ED?logo=docker&logoColor=white)](https://github.com/users/kanlas-net/packages/container/package/terraform-mirror)
[![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A caching **mirror** for the two protocols the Terraform CLI reads from a registry:

* the **module registry** (registry protocol v1) — version lists, `X-Terraform-Get` and the
  module archives themselves;
* the **provider network mirror** — `index.json`, `<version>.json` and the provider packages,
  with `h1:`/`zh:` hashes so `terraform init` verifies what it installs.

An answer comes out of the mirror's own storage; a miss is fetched once from the upstream registry
and cached. Storage is a local directory, an S3-compatible bucket, or both tiers at once, and any
number of instances can share it — a download is coordinated with lock files, so one archive is
fetched once. Documents are revalidated on a TTL, but a cache that has the bytes keeps serving
them while the upstream is away.

Standard library Python only: not a single third-party runtime dependency (neither FastAPI nor
boto3 — the S3 V4 signature is implemented in `terraform_mirror/sigv4.py`). The Python package is
`terraform_mirror`, the command is `terraform-mirror`, every setting starts with `TFM_*`.

## How it works

```
terraform init
   │
   │  modules
   │  GET /v1/modules/<ns>/<name>/<prov>/versions
   ├─► mirror ──► registry.terraform.io       (TTL cache, stale-while-revalidate)
   │  GET /v1/modules/<ns>/<name>/<prov>/<ver>/download
   ├─► mirror: 204 + X-Terraform-Get: https://mirror/archive/<ns>/<name>/<prov>/<ver>.zip
   │            └── a queue job: download the archive from the origin
   │  GET /archive/<ns>/<name>/<prov>/<ver>.zip
   ├─► mirror: bytes from fs → S3 → (if missing) a synchronous download from the origin
   │
   │  providers
   │  GET /<host>/<ns>/<type>/index.json
   ├─► mirror ──► registry.terraform.io       (TTL cache)
   │  GET /<host>/<ns>/<type>/<ver>.json
   ├─► mirror: platforms we host, with h1:/zh: hashes (a miss is fetched now)
   │  GET /<host>/<ns>/<type>/terraform-provider-…zip
   └─► mirror: bytes from fs → S3 → (if missing) a synchronous download from the origin
```

* Version lists, `X-Terraform-Get`, provider `index.json` and `<version>.json` are cached as
  documents with a TTL.
* An archive is downloaded **once**: a background job for modules, a blocking fetch for a provider
  package, deduplicated by key, with a local per-process lock and a distributed lock file in S3
  shared between instances.
* A non-`http(s)` origin (for example `git::https://…`) is forwarded as it is — such an
  archive cannot be cached.

## Quick start

### Docker

The prebuilt image lives in the GitHub Container Registry and is public — no login needed:

```bash
docker run -d --name terraform-mirror -p 8080:8080 \
  -e TFM_BACKENDS=fs \
  -v terraform-mirror-cache:/var/cache/terraform-mirror \
  ghcr.io/kanlas-net/terraform-mirror:1.0.0
curl -s localhost:8080/healthz
```

Tags of the same image: `1.0.0` (exact version), `1.0` (minor), `1` (major),
`latest`. Built for `linux/amd64` and `linux/arm64`.

Build from source:

```bash
docker build -t terraform-mirror .
docker run -d --name terraform-mirror -p 8080:8080 \
  -e TFM_BACKENDS=fs -e TFM_CACHE_DIR=/var/cache/terraform-mirror \
  -v terraform-mirror-cache:/var/cache/terraform-mirror \
  terraform-mirror
curl -s localhost:8080/healthz
```

The tests can also be run inside the image:

```bash
docker build --target test -t terraform-mirror-test . && docker run --rm terraform-mirror-test
```

### Without Docker

```bash
python3 -m terraform_mirror serve --port 8080 --backend fs --cache-dir .tf-cache
python3 -m terraform_mirror prefetch hashicorp/consul/aws          # warm the cache
python3 -m terraform_mirror cache --list                            # what the cache holds
python3 -m terraform_mirror healthcheck                             # probe the storage, exit 1 if broken
python3 -m terraform_mirror ping                                    # probe our own HTTP socket (what the image HEALTHCHECK runs)
python3 -m terraform_mirror info                                    # the resulting configuration
```

### Terraform client configuration

See `examples/terraform-cli-config.tf` (the `TF_CLI_CONFIG_FILE` variable):

```hcl
host "registry.terraform.io" {
  services = {
    modules.v1 = "https://tf.example.com/v1/modules/"
  }
}
```

After that `source = "hashicorp/consul/aws"` keeps working as before, but all the traffic
goes through the proxy. The second option is to use the proxy name directly in the source
(`tf.example.com/hashicorp/consul/aws`): then `services` is not needed, the proxy serves
`/.well-known/terraform.json` itself.

## Configuration

Every option is available both as an environment variable (`TFM_*`) and as a CLI flag
(`terraform-mirror serve --help`).

| Variable | Default | Purpose |
|---|---|---|
| `TFM_HOST` / `TFM_PORT` | `0.0.0.0` / `8080` | listen address |
| `TFM_UPSTREAM_URL` | `https://registry.terraform.io` | the upstream registry |
| `TFM_EXTERNAL_URL` | from the `Host` header | base URL of the proxy, used in `X-Terraform-Get` |
| `TFM_BACKENDS` | `fs` | `fs`, `s3` or `fs,s3` (read: fs → s3, write: both) |
| `TFM_CACHE_DIR` | `./cache` | directory of the disk backend |
| `TFM_TTL_VERSIONS` | `3600` | TTL of the version list, seconds |
| `TFM_TTL_DOWNLOAD` | `604800` | TTL of `X-Terraform-Get`, seconds |
| `TFM_STALE_REVALIDATE` | `true` | serve stale cache and refresh it in the background |
| `TFM_PREFETCH_LATEST` | `true` | on a version request, immediately queue the latest archive |
| `TFM_MAX_ARCHIVE_MB` | `512` | hard archive size limit |
| `TFM_WORKERS` / `TFM_QUEUE_MAX` | `4` / `2000` | background downloaders |
| `TFM_SHUTDOWN_GRACE` | `15` | seconds in-flight downloads get on `SIGTERM`/`SIGINT` before locks are handed back; keep it below the pod's `terminationGracePeriodSeconds` |
| `TFM_REQUEST_TIMEOUT` | `10` | socket timeout for short JSON requests to the registry, seconds |
| `TFM_DOWNLOAD_TIMEOUT` | `60` | socket timeout while downloading an archive (per read, not for the whole download), seconds |
| `TFM_PROMOTE_TO_FS` | `true` | on an S3 hit, warm the disk cache |
| `TFM_LOCKS` | `true` | distributed lock files |
| `TFM_LOCK_TTL` | `900` | lock file lifetime, seconds — the worst case after a `SIGKILL`, not after a normal stop |
| `TFM_LOCK_WAIT` | `120` | how long to wait for someone else's download, seconds |
| `TFM_S3_BUCKET` / `TFM_S3_REGION` | — | bucket and region |
| `TFM_S3_ENDPOINT` | AWS | endpoint for MinIO / Yandex Object Storage / R2 / Ceph |
| `TFM_S3_PREFIX` | `terraform-mirror` | key prefix |
| `TFM_S3_ACCESS_KEY` / `TFM_S3_SECRET_KEY` / `TFM_S3_SESSION_TOKEN` | — | keys (there is a fallback to `AWS_*`) |
| `TFM_S3_PATH_STYLE` | auto for custom endpoints | `https://host/bucket/key` instead of a virtual host |
| `TFM_S3_CONDITIONAL_LOCKS` | `false` | create locks with `If-None-Match: *` |
| `TFM_S3_LOCK_VERIFY_MS` | `250` | pause before reading a lock object back, when conditional writes are unsupported |
| `TFM_S3_UNSIGNED_PAYLOAD` | `false` | `UNSIGNED-PAYLOAD` on PUT (HTTPS only) |
| `TFM_ADMIN_TOKEN` | empty | token for `/api/v1/*` |
| `TFM_TLS_CERT` / `TFM_TLS_KEY` | empty | HTTPS without a reverse proxy |
| `TFM_LOG_LEVEL` | `INFO` | logging |
| `TFM_HEALTH_PROBE_WRITE` | `true` | the storage probe also writes one small object (put → read back → delete) |
| `TFM_HEALTH_CACHE_TTL` | `5` | seconds a readiness result is reused, so kubelet polling does not hammer the bucket |
| `TFM_HEALTH_TIMEOUT` | `5` | socket timeout for probes, seconds (much shorter than a normal object operation) |
| `TFM_STARTUP_CHECK` | `true` | probe the storage once before serving |
| `TFM_STARTUP_CHECK_RETRIES` | `5` | how many attempts the startup probe gets |
| `TFM_STARTUP_CHECK_INTERVAL` | `3` | pause between startup probe attempts, seconds |
| `TFM_STARTUP_REQUIRED` | `false` | exit 1 instead of serving when the startup probe fails |
| `TFM_READY_STRICT` | `true` | `false` = one healthy backend is enough for `/readyz` (degraded but serving) |
| `TFM_READY_UPSTREAM` | `false` | a broken upstream registry also fails `/readyz` |
| `TFM_METRICS_PENDING_SERIES` | `50` | how many per-module pending series `/metrics` exports (`0` = totals only) |
| `TFM_PROVIDER_MIRROR` | `true` | serve the provider network mirror protocol at all |
| `TFM_PROVIDER_UPSTREAM_URL` | `https://registry.terraform.io` | the provider registry the mirror pulls from |
| `TFM_PROVIDER_MIRROR_PREFIX` | empty | serve the mirror under a path instead of the root (see [Provider network mirror](#provider-network-mirror)) |
| `TFM_TTL_PROVIDER_VERSIONS` | `3600` | TTL of a provider version list, seconds |
| `TFM_PROVIDER_PLATFORMS` | `linux_amd64` | comma-separated `os_arch` the mirror advertises |
| `TFM_PROVIDER_BLOCK_WAIT` | `120` | seconds a `:version.json` request waits for a package to be fetched |

## S3 and several instances on one bucket

Locks exist for exactly this scenario. Before downloading an archive, an instance atomically
creates the object `locks/modules/<ns>/<name>/<prov>/<ver>.zip.lock` with a JSON record:

```json
{"token": "…", "key": "modules/…/1.0.0.zip", "instance": "host:pid",
 "started_at": 1793780000.0, "expires_at": 1793780900.0}
```

* created the lock — we download; did not create it — we wait (`TFM_LOCK_WAIT`) until the
  object shows up;
* `expires_at` is in the past → the lock counts as leftovers from a killed process and gets
  taken over (`TFM_LOCK_TTL`), so `kill -9` or an OOM never blocks a module forever;
* a long download refreshes `expires_at` before writing the object;
* only your own lock can be deleted (the `token` is compared), someone else's is left alone;
* after the write (or an error) the lock is released in `finally`;
* a graceful stop (`SIGTERM`, `SIGINT`) does not wait for the TTL either: the process first gives
  in-flight downloads `TFM_SHUTDOWN_GRACE` seconds to finish — each one releases its own lock —
  and then deletes every lock it still holds, so a rolling update never leaves a module locked;
* what is left after `kill -9`, an OOM kill or a grace period that was too short is cleaned up by
  the TTL, and only by the TTL: no signal handler runs on `SIGKILL`, so `TFM_LOCK_TTL` is the
  worst-case time a module stays blocked after a hard kill.

Two ways to create a lock atomically:

1. `TFM_S3_CONDITIONAL_LOCKS=true` — a PUT with `If-None-Match: *`, the storage refuses by
   itself (AWS S3, MinIO, R2, Ceph RGW). Strict mutual exclusion.
2. By default — a PUT, a short pause and a read-back: the lock stays ours only if exactly our
   bytes are in the bucket. Works against any S3-compatible storage; in a rare race there can
   be two "winners", but both download the same bytes and the TTL bounds the consequences.

For the disk backend the lock is created with `O_CREAT|O_EXCL` (atomic), but the disk is local
to each instance, so the S3 tier is always the arbiter.

### Shutting down without leaving a lock behind

`SIGTERM`/`SIGINT` runs this order, and it is the order that matters:

1. stop accepting connections and close the listening socket;
2. wait up to `TFM_SHUTDOWN_GRACE` seconds for in-flight downloads — each one releases its own
   lock in `finally` when it finishes, successfully or not;
3. delete every lock the process still holds (only its own tokens), so a peer can take that
   module over immediately instead of waiting for `TFM_LOCK_TTL`;
4. exit.

`TFM_SHUTDOWN_GRACE=0` skips step 2 and hands the locks back at once. A download that outlives
the grace period is not corrupted by having its lock taken away: the peer downloads the same
bytes, and whichever write lands last is a complete object. The grace period is a budget for the
whole process, not one per worker, and it must stay below the orchestrator's own deadline —
otherwise the process is `SIGKILL`ed in step 2 and only the TTL cleans up.

`/healthz` and `terraform_mirror_locks{name="held"}` show how many locks this instance currently owns; a
number that only ever grows is a leak, and it should be zero a minute after a deploy.

### Re-downloading something that is already cached

`force` re-fetches an archive that the cache already has and overwrites it in every backend. It
also re-resolves `X-Terraform-Get` from the upstream instead of reusing the cached copy, so it
covers the case where the origin moved the archive. Counted as `archive_refetched`.

```bash
curl -sS -X POST -H "x-admin-token: $TOKEN" -H 'content-type: application/json' \
  -d '{"namespace":"hashicorp","name":"consul","provider":"aws","versions":["5.12.0"],"force":true}' \
  https://tf.example.com/api/v1/prefetch

terraform-mirror prefetch --force hashicorp/consul/aws@5.12.0   # the same, one-shot, without a server
```

| Situation | Use |
|---|---|
| the cached bytes are wrong or the upstream re-published the version | `force` — overwrites in place, a client downloading it right now still gets a complete object |
| you want the module gone and refetched on the next request | `DELETE /api/v1/cache/{ns}/{name}/{prov}[/{ver}]` (or `terraform-mirror cache --purge`) |
| the *version list* is stale, not the archive | neither: it expires with `TFM_TTL_VERSIONS`, or purge the module, which drops the document too |
| a 0-byte object is in the cache | nothing — it is reported as missing and refetched by the next request, counted as `archive_broken` |

An interrupted download never leaves a partial object in the first place: the disk backend writes
to a temporary file and `os.replace`s it into place, and S3 gets a single `PUT`. What a killed
process really leaves behind is its lock, which is what the section above is about.

## Endpoints

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/.well-known/terraform.json` | anyone | `{"modules.v1": "/v1/modules/"}` |
| GET | `/v1/modules/{ns}/{name}/{prov}/versions` | anyone | the version list (cached) |
| GET | `/v1/modules/{ns}/{name}/{prov}/{ver}/download` | anyone | `204` + `X-Terraform-Get` |
| GET/HEAD | `/archive/{ns}/{name}/{prov}/{ver}.zip` | anyone | the archive bytes |
| GET | `/{hostname}/{ns}/{type}/index.json` | anyone | provider mirror: the version list of one provider |
| GET | `/{hostname}/{ns}/{type}/{ver}.json` | anyone | provider mirror: platforms and hashes of one version |
| GET/HEAD | `/{hostname}/{ns}/{type}/terraform-provider-{type}-{ver}-{platform}.zip` | anyone | provider mirror: the package bytes |
| GET | `/healthz` | anyone | liveness: status, backends, queue, lock statistics — never touches the network |
| GET | `/readyz` | anyone | readiness: probes the storage backends (and with `TFM_READY_UPSTREAM`, the registry) |
| GET | `/metrics` | anyone | Prometheus text format |
| GET | `/api/v1/cache` | admin | what is cached + counters |
| GET | `/api/v1/pending` | admin | requested module versions that are still not in storage |
| POST | `/api/v1/prefetch` | admin | warm the cache: `{"namespace","name","provider","versions","force"}` |
| DELETE | `/api/v1/cache/{ns}/{name}/{prov}[/{ver}]` | admin | drop a module or a version |

Response headers: `X-TFM-Cache: hit|miss|stale|stale-on-error`, `X-TFM-Source: proxy|upstream`,
`ETag` (the sha256 of the archive), `Retry-After: 5` on `503 download_in_progress`. On the provider
mirror, `X-TFM-Cache` is on all three endpoints: on `index.json` it says whether the version list
came from storage, on `:version.json` whether any package had to be fetched to answer it.

Every answered request also writes one access-log line, with the same information and the response
size:

```text
INFO terraform_mirror.http: 10.0.0.7 "GET /…/terraform-provider-vault_4.4.0_linux_amd64.zip HTTP/1.1" 200 41234567 cache=hit
INFO terraform_mirror.http: 10.0.0.7 "GET /…/hashicorp/vault/index.json HTTP/1.1" 200 168 cache=miss
INFO terraform_mirror.http: 10.0.0.7 "GET /readyz HTTP/1.1" 200 412
```

`cache=hit` means storage alone answered it; `miss` means bytes came from the registry or the
archive origin during this request; `stale` / `stale-on-error` mean a cached copy was served while
the upstream was slow or away. Requests with no cache concept (`/healthz`, `/metrics`, the admin
API) carry no `cache=` field at all. So "did the mirror actually serve from its own storage?" is a
`grep -c 'cache=hit'` away, and the absence of `cache=miss` during a `terraform init` is the proof
that nothing went out.

## Provider network mirror

Besides the module registry protocol the proxy can serve the **provider network mirror** — the
protocol the Terraform CLI speaks when the CLI config says:

```hcl
provider_installation {
  network_mirror {
    url = "https://tf-proxy.example.com/"
  }
}
```

Terraform then asks three questions per provider, all under the root (or under
`TFM_PROVIDER_MIRROR_PREFIX`, which is the only difference in the paths):

```text
GET /:hostname/:namespace/:type/index.json
    {"versions": {"5.13.0": {}, "5.12.0": {}}}
GET /:hostname/:namespace/:type/5.13.0.json
    {"archives": {"linux_amd64": {"url": "terraform-provider-vault_5.13.0_linux_amd64.zip",
                                 "hashes": ["h1:…", "zh:…"]}}}
GET /:hostname/:namespace/:type/terraform-provider-vault_5.13.0_linux_amd64.zip
    the zip, plugin binary at the archive root
```

The archive URL is relative to the `:version.json` document, so the zip sits next to
`index.json` — the same flat layout public mirrors use, which makes the proxy a drop-in
replacement for an existing mirror URL. A `404` on `index.json` is how a mirror says "I do not
carry this provider"; every other status is a protocol error, so an upstream failure answers
`502` instead of quietly claiming the provider does not exist.

What this adds on top of a static mirror:

* **a miss pulls from the registry.** The version list and each package are fetched from
  `TFM_PROVIDER_UPSTREAM_URL`, written to the same backends as modules (`fs`, `s3`, or both) and
  served from storage afterwards. Only platforms in `TFM_PROVIDER_PLATFORMS` *and* published
  upstream for that version are advertised; a package that cannot be fetched is left out of the
  answer rather than served broken.
* **hashes are served, which public mirrors do not bother with.** `h1:` (dirhash of the archive
  contents) and `zh:` (the zip itself) are computed here, over exactly the bytes we hand out, so
  `terraform init` verifies the package instead of trusting the transport. Neither needs a key.
* **archives are flattened.** A `github://owner/repo:ref.zip` source unpacks with everything under
  `repo-ref/`; we repack from the shallowest directory that holds a `terraform-provider-*` file and
  compute the hashes after repacking, so they describe what is served.
* **an upstream outage does not empty the mirror.** A cached version list older than
  `TFM_TTL_PROVIDER_VERSIONS` is still served when the registry is unreachable or has dropped the
  provider; packages already in storage keep serving forever (a provider release never changes).

Warming a cold mirror, so the first `terraform init` is not the one paying for the download:

```bash
terraform-mirror mirror-add hashicorp/vault                      # the newest version
terraform-mirror mirror-add hashicorp/vault@3.22.0 registry.terraform.io/hashicorp/aws@5.42.0
terraform-mirror mirror-add hashicorp/vault --all-versions --platform linux_arm64 --platform linux_amd64
terraform-mirror mirror-list                                     # what is in storage, with h1
```

`mirror-add` and `mirror-list` talk to the same storage the server uses, so run them where
`TFM_BACKENDS`/`TFM_CACHE_DIR`/`TFM_S3_*` point at the mirror's own bucket — inside the pod is the
usual place (`kubectl exec`).

The counter `terraform_mirror_provider_events_total{event}` separates `provider_stored` from
`provider_failed`: the first says a package landed in storage, the second says a client was
offered an answer without that platform.

Pulling on a miss needs a route from the proxy to `TFM_PROVIDER_UPSTREAM_URL`. Registry calls and
archive downloads go through `urllib`, so the usual `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`
environment variables are honoured — there is no dedicated setting; give the container a proxy that
is allowed to reach the registry and the CDN behind it.

Where there is no route at all (an air-gapped registry proxy), keep serving and populate the bucket
from elsewhere with `mirror-add`: fetch on a machine that does have egress, into the same bucket,
and the mirror answers from storage without ever calling out.

## Health checks

Three ways to ask the same question, at three different costs:

| Check | Question | Fails when |
|---|---|---|
| `GET /healthz` | is the process wedged? | nothing but a dead process: it never touches the network |
| `GET /readyz` | can this instance serve a real `terraform init` right now? | a storage backend does not answer (or the registry, with `TFM_READY_UPSTREAM=true`) |
| `terraform-mirror healthcheck` | the same, once, from the command line | exit code 1, JSON report on stdout |
| `terraform-mirror ping` | does this process answer on its own socket? | nothing listens, or `/healthz` (or `/readyz` with `--ready`) is not 2xx |

* A storage probe is a `ListObjectsV2` with `max-keys=1` plus, by default, one small
  write → read back → delete round trip. Read-only credentials pass a read probe and then fail
  every download, which is why the write is on; `TFM_HEALTH_PROBE_WRITE=false` turns it off. The
  probe reports the S3 error code (`AccessDenied`, `NoSuchBucket`, `SignatureDoesNotMatch`) rather
  than a bare HTTP status.
* The write probe writes `health/probe-<host>-<pid>.json`, not one shared object: two replicas on
  one bucket would otherwise delete each other's file and report a read-back mismatch that is not
  real.
* The result is cached for `TFM_HEALTH_CACHE_TTL` seconds, so a kubelet polling every 5 s does not
  put thousands of LIST calls on the bucket. `GET /readyz?force=1` skips the cache.
* The same checks run once at startup, with `TFM_STARTUP_CHECK_RETRIES` attempts, so a bucket that
  is mid-restart gets a chance. By default the proxy starts anyway and only reports "not ready";
  `TFM_STARTUP_REQUIRED=true` makes a failed startup fatal instead.
* `TFM_READY_STRICT=false` keeps an instance in rotation while at least one tier is healthy —
  the disk serving while the bucket is away — instead of taking every pod out at once.

```bash
terraform-mirror healthcheck                # JSON report, exit 1 when something is broken
terraform-mirror healthcheck --pending      # plus what is still waiting to be downloaded
terraform-mirror healthcheck --read-only    # skip the write probe
terraform-mirror ping                       # one HTTP GET against our own socket, exit 1 when not 2xx
```

### The image HEALTHCHECK and TLS

`HEALTHCHECK` in the image runs `terraform-mirror ping`, which builds its own URL from the
environment: `TFM_PORT`, `TFM_HOST` (loopback when it is a wildcard) and the scheme from
`TFM_TLS_CERT`. That matters, because `make_server` wraps *the same* socket in TLS — a
plain `http://127.0.0.1:8080/healthz` probe against a TLS instance dies with
`ConnectionResetError`, and against any other port with `ConnectionRefusedError`, so the
container reads "unhealthy" forever while serving clients perfectly.

```bash
terraform-mirror ping                      # http://127.0.0.1:8080/healthz by default
terraform-mirror ping --ready              # /readyz instead
terraform-mirror ping --url http://localhost:8080/healthz --timeout 2
```

An `https` probe deliberately does not verify the certificate: it is issued for the public
name (`tf.example.com`), the probe connects to loopback, and a self-signed cert is normal
inside a container. The question is "is the socket answering", not "is the chain trusted".
For the same reason Kubernetes probes with `TFM_TLS_*` set need `scheme: HTTPS` on each
`httpGet` (kubelet does not verify certificates either).

### Kubernetes

`examples/kubernetes.yaml` is a working Deployment + Service; the probe block is the part that
matters:

```yaml
startupProbe:                       # not ready until the bucket answers; 30 x 5s budget
  httpGet: {path: /readyz, port: http}
  periodSeconds: 5
  failureThreshold: 30
readinessProbe:
  httpGet: {path: /readyz, port: http}
  periodSeconds: 10
livenessProbe:
  httpGet: {path: /healthz, port: http}
  periodSeconds: 10
```

Keep liveness on `/healthz`: pointing it at `/readyz` turns an object-storage outage into a restart
storm, and the proxy has a warm cache it can serve while the bucket is away.

#### What a killed pod leaves behind

| Leftover | Who cleans it up |
|---|---|
| the `locks/….lock` of the download that was running | released on `SIGTERM`/`SIGINT`; after `SIGKILL` or an OOM kill it expires with `TFM_LOCK_TTL` and the next instance breaks it and takes over |
| a half-written archive | there is none: both backends publish an object atomically (`os.replace` on disk, one `PUT` in S3), so an interrupted download leaves no object; a zero-byte object that got there some other way is reported as missing and refetched on the next request |
| the disk cache | an `emptyDir` dies with the pod, so a restarted pod simply refetches from the bucket; with a PVC under `TFM_CACHE_DIR` the leftovers survive and the two rows above apply |

Give the pod a grace period larger than the shutdown budget, otherwise kubelet sends `SIGKILL`
before the locks are handed back:

```yaml
terminationGracePeriodSeconds: 30      # must be greater than TFM_SHUTDOWN_GRACE (15)
```

The order the pod actually runs on `SIGTERM` is in
[Shutting down without leaving a lock behind](#shutting-down-without-leaving-a-lock-behind), and
how to re-download something the cache should not be trusted on is in
[Re-downloading something that is already cached](#re-downloading-something-that-is-already-cached).

## Metrics

`GET /metrics` serves Prometheus text format 0.0.4, written by hand (`terraform_mirror/metrics.py`) — the
image has no client library to keep in step with the server.

| Series | Type | What it answers |
|---|---|---|
| `terraform_mirror_upstream_requests_total{op,result}` | counter | calls to the registry, `result` = `ok`, `error`, `not_found` |
| `terraform_mirror_upstream_errors_total{op,reason}` | counter | failed registry calls, `reason` = `unreachable`, `http_503`, `malformed`, … |
| `terraform_mirror_upstream_consecutive_failures{op}` | gauge | failures in a row, reset by the next success |
| `terraform_mirror_upstream_last_success_timestamp_seconds{op}` | gauge | when the registry last answered |
| `terraform_mirror_upstream_request_seconds{op}` | histogram | how long the registry takes |
| `terraform_mirror_archive_pending_modules` | gauge | versions clients asked for that are still not in storage |
| `terraform_mirror_archive_pending_oldest_seconds` | gauge | how long the oldest of them has been waiting |
| `terraform_mirror_archive_pending_age_seconds{module,version}` | gauge | the same per module (capped by `TFM_METRICS_PENDING_SERIES`) |
| `terraform_mirror_archive_downloads_total{result}` | counter | archive fetches from the origin: `ok`, `error`, `store_error`, `not_cacheable` |
| `terraform_mirror_archive_download_seconds` | histogram | time to fetch and store one archive |
| `terraform_mirror_http_requests_total{route,method,status}` | counter | everything the proxy answered, by route and status class |
| `terraform_mirror_http_errors_total{route,code}` | counter | 4xx/5xx answers, labelled with the protocol error code |
| `terraform_mirror_http_request_seconds{route}` | histogram | request latency per route |
| `terraform_mirror_storage_probe_up{backend}`, `terraform_mirror_storage_probe_seconds{backend}` | gauge | last probe result per backend |
| `terraform_mirror_storage_errors_total{op}` | counter | storage failures the proxy swallowed instead of failing a request |
| `terraform_mirror_jobs{name}`, `terraform_mirror_locks{name}` | gauge | background queue and distributed lock counters; `terraform_mirror_locks{name="held"}` is how many locks this instance still owns |
| `terraform_mirror_provider_events_total{event}` | counter | provider mirror: `provider_stored`, `provider_failed` |
| `terraform_mirror_cache_events_total{kind,status}` | counter | answered requests by what served them: `kind` = `module`/`provider`, `status` = `hit`, `miss`, `stale`, `stale-on-error`, `refresh` |

How much of the traffic the cache answered, over the last hour:

```promql
sum by (kind, status) (increase(terraform_mirror_cache_events_total[1h]))

# the hit ratio, one number per protocol
sum by (kind) (increase(terraform_mirror_cache_events_total{status="hit"}[1h]))
  / sum by (kind) (increase(terraform_mirror_cache_events_total[1h]))
```

Only requests that transfer a body are counted — Terraform sends a `HEAD` before every provider
download, and counting it would double each install in the ratio. Errors carry no cache status and
are not counted at all, so the denominator is "answers that served something". The same values are
in the access log (`cache=hit`) and in `X-TFM-Cache`, one request at a time; the counter is the
aggregate over time.
| `terraform_mirror_uptime_seconds`, `terraform_mirror_process_threads`, `terraform_mirror_process_max_rss_bytes`, `terraform_mirror_build_info{version}` | gauge | the process itself |
| `terraform_mirror_versions_hit`, `terraform_mirror_archive_fetches`, … | counter | the cache counters also visible in `/api/v1/cache` |

Three alerts worth wiring up first (`examples/prometheus-alerts.yaml` has them ready):

```yaml
# 1. the upstream registry is failing us — 404s are deliberately not counted here
- alert: TerraformRegistryUnreachable
  expr: increase(terraform_mirror_upstream_errors_total[5m]) > 3

# 2. nothing has come from the registry for 5 minutes, even at low traffic
- alert: TerraformRegistrySilent
  expr: time() - terraform_mirror_upstream_last_success_timestamp_seconds > 300

# 3. someone asked for a module and 10 minutes later it still is not in storage
- alert: TerraformModulesNotDownloaded
  expr: terraform_mirror_archive_pending_oldest_seconds > 600
```

`terraform_mirror_archive_pending_modules > 0` is the softer version of the third alert: it says "a download
is outstanding", which is normal for a few seconds and a problem after ten minutes.

`/metrics` is unauthenticated, because Prometheus scrapes without credentials. Every series above is
aggregate except `terraform_mirror_archive_pending_age_seconds{module,version}`, which names the modules you
serve — that one is opt-in (`TFM_METRICS_PENDING_SERIES`, default `0` = totals only). Keep the scrape
port inside the cluster network, or front it with whatever your Prometheus uses to authenticate.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

The fakes the tests bring up are real HTTP servers: `tests/support/fake_upstream.py`
(a registry plus archive serving) and `tests/support/fake_s3.py` (PUT/GET/HEAD/DELETE/
ListObjectsV2, checks `Authorization`, `x-amz-content-sha256` and `If-None-Match`).

Checked separately from the rest:

* `test_sigv4.py` — the S3 V4 signature against reference vectors computed by
  `botocore.auth.SigV4Auth` (with botocore installed, a per-file comparison against botocore
  is added);
* `test_locking.py` — two `LockManager` instances on one storage: exactly one winner, taking
  over an expired lock, `refresh`, `wait_for_release`;
* `test_server_registry.py::SharedS3Test` — two proxy instances on one bucket: the archive is
  downloaded once, the second instance serves it from S3, a parallel download is blocked;
* `test_cache.py` — TTL, stale-while-revalidate, `stale-on-error`, deduplication of background
  jobs;
* `test_providers.py` — the provider network mirror: `h1:`/`zh:` against an independent dirhash
  implementation, archive flattening, the upstream client against `tests/support/fake_provider_registry.py`,
  and the three mirror endpoints through the proxy itself.

## Versioning and builds

The version lives in two places and must match: `terraform_mirror.__version__` and `version` in
`pyproject.toml` (`tests/test_packaging.py` makes sure of that).

A release is a git tag `vX.Y.Z`. To ship one:

```bash
# 1. bump the version in terraform_mirror/__init__.py and pyproject.toml, and move the
#    "Unreleased" block of CHANGELOG.md under a "## [X.Y.Z]" heading
git commit -am "release 1.2.0"
git push origin main

# 2. tag it and push the tag explicitly — a lightweight tag is NOT carried by --follow-tags
git tag -a v1.2.0 -m "release 1.2.0"
git push origin refs/tags/v1.2.0
```

GitHub Actions does the rest:

| Workflow | When | What it does |
|---|---|---|
| `ci.yml` | push to `main`, PR, manual run | tests on Python 3.10–3.13 with the optional extras installed (SigV4 cross-check, TLS probe tests), image build, `--target test` build with the suite run inside the image, smoke test of `/healthz` and `/.well-known/terraform.json`, and a wait for the image's own `HEALTHCHECK` to go healthy over plain http and over TLS |
| `release.yml` | tag `vX.Y.Z` | compares the tag with `terraform_mirror.__version__`, runs the tests, builds `linux/amd64` + `linux/arm64`, publishes to `ghcr.io`, creates the GitHub Release |

Publishing runs under the service `GITHUB_TOKEN` (with `packages: write`); the repository needs
no extra secrets. Package visibility follows the repository — for a public repository the image
is public and is pulled without `docker login`.

Semver as used here:

* **MAJOR** — breaks the client or the configuration: a different protocol path, a `TFM_*` variable that was renamed or
  changed meaning, a different key format in the bucket;
* **MINOR** — backwards compatible: a new endpoint, a new variable, a new backend;
* **PATCH** — fixes only, nothing got worse for the client.

The image in the registry: `ghcr.io/kanlas-net/terraform-mirror`.

## Limitations

* The **module** protocol only. Providers (`/v1/providers/...`, `SHA256SUMS`, key signatures)
  are not proxied — `getproviders` mirrors exist for those.
* Terraform requires HTTPS for external hosts; for production add TLS
  (`TFM_TLS_CERT`/`TFM_TLS_KEY` or a reverse proxy). `http://` is convenient only for local
  experiments on `127.0.0.1`.
* Range requests for archives are not supported: an archive is always served in full, its size
  is known in advance.
* Metadata cache entries and archives are plain objects: if you want, you can read them straight
  from the bucket, but keep the `meta/…` and `locks/…` paths inside the proxy.
* Background downloads do not survive a process restart: unfinished jobs are queued again on the
  next client request.
