"""A fake upstream *provider registry* (not a mirror) for the provider tests.

It speaks the two shapes the real registry is known to answer with: the JSON body at
``/v1/providers/:ns/:type/:version/download/:os/:arch`` and the older ``204`` plus
``X-Terraform-Get`` at ``…/download?os=&arch=``. Release archives are served from the
same server so a test can point ``download_url`` at itself.
"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_package(name: str, nested: str = "") -> bytes:
    """Build a provider zip. ``nested`` wraps everything, like a GitHub archive does."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        prefix = f"{nested}/" if nested else ""
        archive.writestr(f"{prefix}{name}", b"fake plugin binary\n")
        archive.writestr(f"{prefix}LICENSE.txt", b"license text\n")
    return buffer.getvalue()


class FakeProviderRegistry:
    """``add(...)`` a provider, then read ``base_url``."""

    def __init__(self) -> None:
        self.providers: dict[str, dict] = {}
        self.packages: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.sha256 = ""  # filled lazily so a test can serve a wrong checksum
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # pragma: no cover - silence
                pass

            def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", extra=None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                path = self.path.split("?", 1)[0]
                outer.requests.append(("GET", self.path))
                if path.startswith("/files/"):
                    blob = outer.packages.get(path[len("/files/") :])
                    if blob is None:
                        return self._send(404, b"{}")
                    return self._send(200, blob, "application/zip")

                parts = [part for part in path.strip("/").split("/") if part]
                # /v1/providers/<ns>/<type>/versions
                if len(parts) == 5 and parts[:2] == ["v1", "providers"] and parts[4] == "versions":
                    provider = f"{parts[2]}/{parts[3]}"
                    row = outer.providers.get(provider)
                    if row is None:
                        return self._send(404, json.dumps({"errors": []}).encode())
                    return self._send(200, json.dumps({"id": provider, "versions": row}).encode())

                # /v1/providers/<ns>/<type>/<version>/download/<os>/<arch>
                if len(parts) == 8 and parts[:2] == ["v1", "providers"] and parts[5] == "download":
                    provider, version, os_name, arch = f"{parts[2]}/{parts[3]}", parts[4], parts[6], parts[7]
                    entry = outer._package(provider, version, f"{os_name}_{arch}")
                    if entry is None:
                        return self._send(404, json.dumps({"errors": []}).encode())
                    filename, blob = entry
                    digest = outer.digest_for(filename, blob)
                    return self._send(
                        200,
                        json.dumps(
                            {
                                "protocols": ["5.0"],
                                "os": os_name,
                                "arch": arch,
                                "filename": filename,
                                "download_url": f"{outer.base_url}/files/{filename}",
                                "shasum": digest,
                            }
                        ).encode(),
                    )

                # /v1/providers/<ns>/<type>/<version>/download?os=&arch=  (X-Terraform-Get shape)
                if len(parts) == 7 and parts[:2] == ["v1", "providers"] and parts[5] == "download":
                    query = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&") if "=" in pair)
                    provider, version = f"{parts[2]}/{parts[3]}", parts[4]
                    entry = outer._package(provider, version, f"{query.get('os')}_{query.get('arch')}")
                    if entry is None:
                        return self._send(404, b"{}")
                    filename, _blob = entry
                    return self._send(
                        204, b"", "application/json", {"X-Terraform-Get": f"https://example.invalid/{filename}"}
                    )
                return self._send(404, json.dumps({"errors": []}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeProviderRegistry":
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---- fixtures ----------------------------------------------------------
    def add(
        self,
        provider: str,
        versions=("1.0.0",),
        platforms=("linux_amd64",),
        *,
        nested: str = "",
        getter_shape: bool = False,
    ) -> "FakeProviderRegistry":
        """Register ``namespace/type`` with the given versions and platforms."""
        self.providers.setdefault(provider, [])
        for version in versions:
            for platform in platforms:
                filename = f"terraform-provider-{provider.split('/')[1]}_{version}_{platform}.zip"
                self.packages[filename] = make_package(f"terraform-provider-{provider.split('/')[1]}_{version}", nested)
            self.providers[provider].append(
                {
                    "version": version,
                    "protocols": ["5.0"],
                    "platforms": [{"os": os_name, "arch": arch} for os_name, arch in (p.split("_", 1) for p in platforms)],
                }
            )
        self.getter_shape = getter_shape
        return self

    def _package(self, provider: str, version: str, platform: str) -> "tuple[str, bytes] | None":
        name = provider.split("/")[1]
        filename = f"terraform-provider-{name}_{version}_{platform}.zip"
        blob = self.packages.get(filename)
        return (filename, blob) if blob is not None else None

    def digest_for(self, filename: str, blob: bytes) -> str:
        """The ``shasum`` the registry advertises; a test can break it on purpose."""
        if self.sha256:
            return self.sha256
        import hashlib

        return hashlib.sha256(blob).hexdigest()

    def count(self, needle: str) -> int:
        return len([row for row in self.requests if needle in row[1]])
