# terraform-mirror — a caching mirror for the Terraform module and provider protocols.
# No third-party runtime dependencies: the image only needs a Python interpreter.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TFM_HOST=0.0.0.0 \
    TFM_PORT=8080 \
    TFM_CACHE_DIR=/var/cache/terraform-mirror

RUN groupadd --system terraform_mirror \
    && useradd --system --gid terraform_mirror --home-dir /var/cache/terraform-mirror --shell /usr/sbin/nologin terraform_mirror

WORKDIR /app
COPY terraform_mirror/ /app/terraform_mirror/

RUN mkdir -p "$TFM_CACHE_DIR" && chown -R terraform_mirror:terraform_mirror /app "$TFM_CACHE_DIR"

USER terraform_mirror
EXPOSE 8080

# The container is healthy when its own /healthz answers 200. `terraform-mirror ping` derives the
# scheme, host and port from the environment: with TFM_TLS_* the very same socket is
# wrapped in TLS (a plain http probe would get ECONNRESET and the container would stay
# "unhealthy" while serving clients perfectly), and a hardcoded 8080 is wrong for any
# other TFM_PORT. The probe does not verify the certificate — see terraform_mirror/health.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/usr/local/bin/python", "-m", "terraform_mirror", "ping"]

ENTRYPOINT ["/usr/local/bin/python", "-m", "terraform_mirror", "serve"]

# --------------------------------------------------------------------------
# Test stage: docker build --target test -t terraform-mirror-test . && docker run --rm terraform-mirror-test
# --------------------------------------------------------------------------
FROM python:3.12-slim AS test

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY . /src
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
