"""Minimal AWS Signature Version 4 signer (S3 flavour), standard library only.

Implements just enough of the spec to sign S3 REST calls against AWS S3 and the
S3-compatible services people actually self-host (Yandex Object Storage, MinIO,
Cloudflare R2, Ceph RGW, LocalStack).

Reference: "Signature Version 4 signing process",
https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from typing import Iterable, Mapping, Optional, Tuple

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


def uri_encode(value: str, encode_slash: bool = True) -> str:
    """RFC 3986 percent-encoding as required by SigV4.

    ``encode_slash=False`` is used for the path, where ``/`` is a separator and
    must stay literal. The value is assumed to be a *raw* (not pre-encoded) string.
    """
    out: list[str] = []
    for char in value:
        if char in _UNRESERVED:
            out.append(char)
        elif char == "/" and not encode_slash:
            out.append("/")
        else:
            out.extend("%%%02X" % byte for byte in char.encode("utf-8"))
    return "".join(out)


def canonical_uri(path: str) -> str:
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return uri_encode(path, encode_slash=False)


def canonical_querystring(query: Mapping[str, str] | str | None) -> str:
    """Build the canonical query string.

    A mapping holds *raw* (not yet encoded) values and is encoded here. A string is
    assumed to be canonical already and is returned untouched, so a caller can build the
    request URL and the signature from one and the same string instead of encoding twice.
    """
    if not query:
        return ""
    pairs: Iterable[Tuple[str, str]]
    if isinstance(query, str):
        return query
    pairs = list(query.items())
    encoded = sorted((uri_encode(str(k), True), uri_encode(str(v), True)) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in encoded)


def _canonical_headers(headers: Mapping[str, str]) -> Tuple[str, str]:
    lowered = {str(k).lower(): " ".join(str(v).split()) for k, v in headers.items() if v is not None}
    names = sorted(lowered)
    canonical = "".join(f"{name}:{lowered[name]}\n" for name in names)
    return canonical, ";".join(names)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret_key).encode("utf-8")
    key = _sign(key, date_stamp)
    key = _sign(key, region)
    key = _sign(key, service)
    return _sign(key, "aws4_request")


def build_canonical_request(
    method: str,
    path: str,
    query: Mapping[str, str] | str | None,
    headers: Mapping[str, str],
    payload_hash: str,
) -> Tuple[str, str]:
    """Return ``(canonical_request, signed_headers)``."""
    canonical_headers, signed_headers = _canonical_headers(headers)
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri(path),
            canonical_querystring(query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    return canonical_request, signed_headers


def sign_request(
    method: str,
    url: str,
    *,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    session_token: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    payload_hash: str = EMPTY_SHA256,
    timestamp: Optional[dt.datetime] = None,
    canonical_path: Optional[str] = None,
    canonical_query: Optional[str] = None,
) -> dict[str, str]:
    """Sign a request and return the headers to add to it.

    ``headers`` must already contain ``host`` (and ``content-length`` for uploads);
    the returned dict contains ``x-amz-date``, ``x-amz-content-sha256``,
    ``authorization`` and, when given, ``x-amz-security-token``.

    ``canonical_path``/``canonical_query`` let the caller reuse the exact percent-encoded
    path and query that went into *url*; by default they are taken from *url* itself,
    which assumes the URL has not been percent-encoded yet.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"cannot sign a request without a host: {url!r}")

    moment = timestamp or dt.datetime.now(dt.timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")
    host = parts.netloc if _has_port(parts.netloc, parts.scheme) else parts.hostname

    to_sign: dict[str, str] = {"host": host}
    for key, value in (headers or {}).items():
        to_sign[str(key).lower()] = value
    to_sign["x-amz-date"] = amz_date
    if service == "s3":
        to_sign.setdefault("x-amz-content-sha256", payload_hash)
    if session_token:
        to_sign["x-amz-security-token"] = session_token

    canonical_request, signed_headers = build_canonical_request(
        method,
        canonical_path if canonical_path is not None else (parts.path or "/"),
        canonical_query if canonical_query is not None else parts.query,
        to_sign,
        payload_hash,
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    auth = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    result = dict(to_sign)
    result["authorization"] = auth
    return result


def _has_port(netloc: str, scheme: str) -> bool:
    if ":" not in netloc:
        return False
    port = netloc.rsplit(":", 1)[1]
    default = {"http": "80", "https": "443"}.get(scheme)
    return port != default


def sha256_file(path: str, chunk_size: int = 1 << 20) -> Tuple[str, int]:
    """Return ``(hex_digest, size)`` for a file on disk."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size
